"""Bind each MCP session to the principal that opened it (NFR-DEP-07, NFR-SEC-02).

The SDK keys a session on its ``mcp-session-id`` alone, so anyone who learns an id could
replay it. This wrapper records ``session id -> (principal type, subject)`` when a response
carries a new id and refuses any request for that id from another principal. Unknown ids,
which the SDK would answer in its own shape, get the same typed ``404`` here, so a foreign
id cannot be told from an unknown one. Entries go on ``DELETE`` or after ``idle_timeout``
seconds without use (the SDK's own default is 1800). Only ids are held, never tokens.

With stateless MCP (``LAKELOOM_MCP_STATELESS``) no ids exist and the wrapper is built
disabled, a pass-through.
"""

import time
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lakeloom.server.auth import Principal
from lakeloom.server.http_api import error_response

__all__ = ["SESSION_HEADER", "SessionBindingMiddleware"]

SESSION_HEADER = b"mcp-session-id"
Owner = tuple[str, str]


class SessionBindingMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        idle_timeout: float = 1800,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool = True,
    ) -> None:
        self.app = app
        self.idle_timeout = idle_timeout
        self.clock = clock
        self.enabled = enabled
        self._bound: dict[str, tuple[Owner, float]] = {}

    @staticmethod
    def _owner(scope: Scope) -> Owner | None:
        state: dict[str, Any] = scope.get("state") or {}
        principal = state.get("principal")
        if not isinstance(principal, Principal):
            return None
        return (principal.type.value, principal.subject)

    def _expire(self, now: float) -> None:
        for sid in [s for s, (_, seen) in self._bound.items() if now - seen > self.idle_timeout]:
            del self._bound[sid]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        owner = self._owner(scope) if scope["type"] == "http" and self.enabled else None
        if owner is None:
            await self.app(scope, receive, send)
            return

        now = self.clock()
        self._expire(now)
        sid_raw = dict(scope["headers"]).get(SESSION_HEADER)
        sid = sid_raw.decode("latin-1") if sid_raw is not None else None
        if sid is not None:
            bound = self._bound.get(sid)
            if bound is None or bound[0] != owner:
                await error_response(
                    "not_found", "unknown MCP session", "initialize a new MCP session"
                )(scope, receive, send)
                return
            self._bound[sid] = (owner, now)
        is_delete = scope["method"] == "DELETE"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                if sid is None and status < 400:
                    for name, value in message.get("headers", []):
                        if name.lower() == SESSION_HEADER:
                            self._bound[value.decode("latin-1")] = (owner, self.clock())
                elif sid is not None and is_delete and status < 400:
                    self._bound.pop(sid, None)
            await send(message)

        await self.app(scope, receive, send_wrapper)
