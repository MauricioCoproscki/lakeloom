"""MCP sessions are bound to the principal that opened them (NFR-DEP-07, NFR-SEC-02)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from kit import AuthKit, Clock, DepsFactory
from starlette.types import ASGIApp, Receive, Scope, Send
from test_tools_semantic import fake_deps

from lakeloom.server.auth import Principal, PrincipalType
from lakeloom.server.mcp_app import create_server_app
from lakeloom.server.mcp_sessions import SessionBindingMiddleware

HEADER = "mcp-session-id"
UNKNOWN = {
    "error": {
        "code": "not_found",
        "message": "unknown MCP session",
        "hint": "initialize a new MCP session",
    }
}


class FakeMcp:
    """Stands in for the SDK: a POST without an id opens session ``s1``; DELETE ends it."""

    def __init__(self) -> None:
        self.seen: list[str | None] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = dict(scope["headers"])
        sid = headers.get(HEADER.encode())
        self.seen.append(sid.decode() if sid else None)
        out: list[tuple[bytes, bytes]] = []
        if sid is None:
            out.append((HEADER.encode(), b"s1"))
        await send({"type": "http.response.start", "status": 200, "headers": out})
        await send({"type": "http.response.body", "body": b"ok"})


def as_principal(app: ASGIApp) -> ASGIApp:
    """What BearerAuthMiddleware does: the ``x-who`` header names the principal."""

    async def run(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            who = dict(scope["headers"])[b"x-who"].decode()
            scope.setdefault("state", {})["principal"] = Principal(
                subject=who,
                type=PrincipalType.AGENT,
                client_id=None,
                username=None,
                scopes=frozenset(),
                groups=frozenset(),
                token=None,  # pyright: ignore[reportArgumentType]
            )
        await app(scope, receive, send)

    return run


@pytest.fixture
def inner() -> FakeMcp:
    return FakeMcp()


@pytest.fixture
async def client(inner: FakeMcp, clock: Clock) -> AsyncIterator[httpx2.AsyncClient]:
    app = as_principal(SessionBindingMiddleware(inner, clock=clock))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://lakeloom.test"
    ) as client:
        yield client


async def open_session(client: httpx2.AsyncClient, who: str = "a") -> str:
    r = await client.post("/mcp", headers={"x-who": who})
    return r.headers[HEADER]


async def use(client: httpx2.AsyncClient, sid: str, who: str, method: str = "POST") -> Any:
    return await client.request(method, "/mcp", headers={"x-who": who, HEADER: sid})


async def test_session_bound_on_initialize(client: httpx2.AsyncClient, inner: FakeMcp) -> None:
    sid = await open_session(client)

    assert sid == "s1"
    assert (await use(client, sid, "a")).status_code == 200
    assert inner.seen == [None, "s1"]


async def test_session_refused_to_other_principal(
    client: httpx2.AsyncClient, inner: FakeMcp
) -> None:
    sid = await open_session(client, "a")

    r = await use(client, sid, "b")

    assert r.status_code == 404
    assert r.json() == UNKNOWN
    assert inner.seen == [None]  # the SDK never saw it


async def test_unknown_and_foreign_sessions_look_alike(client: httpx2.AsyncClient) -> None:
    sid = await open_session(client, "a")

    foreign = await use(client, sid, "b")
    unknown = await use(client, "never-issued", "b")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json() == UNKNOWN
    assert foreign.headers["content-type"] == unknown.headers["content-type"]


async def test_owner_keeps_using_session(client: httpx2.AsyncClient) -> None:
    sid = await open_session(client, "a")
    await use(client, sid, "b")  # a thief's attempt

    assert [(await use(client, sid, "a")).status_code for _ in range(3)] == [200] * 3


async def test_delete_unbinds(client: httpx2.AsyncClient) -> None:
    sid = await open_session(client)

    assert (await use(client, sid, "a", "DELETE")).status_code == 200
    assert (await use(client, sid, "a")).status_code == 404


async def test_foreign_delete_does_not_unbind(client: httpx2.AsyncClient) -> None:
    sid = await open_session(client, "a")

    assert (await use(client, sid, "b", "DELETE")).status_code == 404
    assert (await use(client, sid, "a")).status_code == 200


async def test_idle_binding_expires(client: httpx2.AsyncClient, clock: Clock) -> None:
    sid = await open_session(client)
    clock.advance(1700)
    assert (await use(client, sid, "a")).status_code == 200  # use renews it

    clock.advance(1700)
    assert (await use(client, sid, "a")).status_code == 200

    clock.advance(1801)
    assert (await use(client, sid, "a")).status_code == 404


async def test_pass_through_when_disabled(inner: FakeMcp) -> None:
    app = as_principal(SessionBindingMiddleware(inner, enabled=False))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="https://lakeloom.test"
    ) as client:
        r = await use(client, "anything", "a")

    assert r.status_code == 200
    assert inner.seen == ["anything"]


ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}


async def test_full_app_foreign_session_is_404(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(fake_deps(make_deps))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://lakeloom.test"
        ) as http,
    ):
        a = {"Authorization": f"Bearer {kit.token(sub='a')}", **ACCEPT}
        b = {"Authorization": f"Bearer {kit.token(sub='b')}", **ACCEPT}
        opened = await http.post("/mcp", headers=a, json=INIT)
        sid = opened.headers[HEADER]

        theirs = await http.post("/mcp", headers={**b, HEADER: sid}, json=INIT)
        unknown = await http.post("/mcp", headers={**b, HEADER: "nope"}, json=INIT)
        mine = await http.post("/mcp", headers={**a, HEADER: sid}, json=INIT)

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json() == UNKNOWN
    assert mine.status_code != 404


async def test_stateless_mode_has_no_session_header(make_deps: DepsFactory, kit: AuthKit) -> None:
    app = create_server_app(fake_deps(make_deps, mcp_stateless=True))
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "list_models", "arguments": {}},
    }
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="https://lakeloom.test"
        ) as http,
    ):
        headers = {"Authorization": f"Bearer {kit.token()}", **ACCEPT}
        init = await http.post("/mcp", headers=headers, json=INIT)
        result = await http.post("/mcp", headers=headers, json=call)

    assert init.status_code == 200
    assert HEADER not in init.headers
    assert init.headers["content-type"].startswith("application/json")
    assert result.status_code == 200
    assert result.json()["result"]["isError"] is False
