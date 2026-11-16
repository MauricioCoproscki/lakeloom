"""Bind each MCP session to the principal that opened it (NFR-DEP-07, NFR-SEC-02).

The SDK keys a session on its ``mcp-session-id`` alone, so anyone who learns an id could
replay it. This wrapper records ``session id -> (principal type, subject)`` when a response
carries a new id and refuses any request for that id from another principal. Unknown ids,
which the SDK would answer in its own shape, get the same typed ``404`` here, so a foreign
id cannot be told from an unknown one. Entries go on ``DELETE`` or after ``idle_timeout``
seconds with no request in flight (the SDK's own meaning of ``session_idle_timeout``, default
1800: an open GET stream or a long tool call keeps the session alive). Only ids are held,
never tokens. A session the SDK has already reaped gives its owner the SDK's own 404. A
request with no principal passes through (only possible outside ``BearerAuthMiddleware``).

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
PRUNE_EVERY = 60.0


class _Binding:
    __slots__ = ("inflight", "owner", "seen")

    def __init__(self, owner: Owner, seen: float) -> None:
        self.owner = owner
        self.seen = seen
        self.inflight = 0


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
        self._bound: dict[str, _Binding] = {}
        self._pruned_at = clock()

    @staticmethod
    def _owner(scope: Scope) -> Owner | None:
        state: dict[str, Any] = scope.get("state") or {}
        principal = state.get("principal")
        if not isinstance(principal, Principal):
            return None
        return (principal.type.value, principal.subject)

    def _expire(self, now: float) -> None:
        """Drop idle bindings (none in flight, unused past the timeout), at most once a minute."""
        if now - self._pruned_at < PRUNE_EVERY:
            return
        self._pruned_at = now
        for sid in [
            s
            for s, b in self._bound.items()
            if b.inflight == 0 and now - b.seen > self.idle_timeout
        ]:
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
        binding: _Binding | None = None
        if sid is not None:
            binding = self._bound.get(sid)
            # An idle binding not yet pruned still counts as expired.
            if (
                binding is None
                or binding.owner != owner
                or (binding.inflight == 0 and now - binding.seen > self.idle_timeout)
            ):
                if binding is not None and binding.owner == owner:
                    del self._bound[sid]
                await error_response(
                    "not_found", "unknown MCP session", "initialize a new MCP session"
                )(scope, receive, send)
                return
            binding.inflight += 1
        is_delete = scope["method"] == "DELETE"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                if sid is None and status < 400:
                    for name, value in message.get("headers", []):
                        if name.lower() == SESSION_HEADER:
                            self._bound[value.decode("latin-1")] = _Binding(owner, self.clock())
                elif sid is not None and is_delete and status < 400:
                    self._bound.pop(sid, None)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if binding is not None:
                binding.inflight -= 1
                binding.seen = self.clock()
