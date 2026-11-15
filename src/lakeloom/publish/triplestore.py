"""Optional SPARQL 1.1 Graph Store sink for releases (FR-TS-01, FR-TS-02, FR-TS-03).

A done release stored with ``sink_status = pending`` is pushed to the configured triple store
(``PUT {url}?graph=<iri>``, ``Content-Type: text/turtle``, HTTP basic when a username is set):

1. ``ontology.ttl`` to ``<{base}release/{version}>``;
2. the same body to ``<{base}latest>``, only while this is the highest release of the model;
3. ``shapes.ttl`` to ``<{base}shapes>``, when shapes are enabled and the release has the file.

The sink never blocks or undoes the Git release: failures are recorded on the release row and
retried with backoff (60 s doubling, max 1 h) until :data:`MAX_SINK_ATTEMPTS`, then the row is
``failed`` until ``lakeloom release sink-retry``. Every PUT replaces a graph, so a retry after a
partial run is safe. The password is stripped from every message (NFR-SEC-11).
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from lakeloom.config import Settings
from lakeloom.core.model_policy import ModelPolicyEntry, triplestore_for
from lakeloom.core.models import ModelService
from lakeloom.errors import LakeloomError
from lakeloom.gitstore.git import GitError, redact
from lakeloom.gitstore.publisher import backoff_seconds
from lakeloom.gitstore.release_reader import ReleaseReader
from lakeloom.state.releases import Release, ReleaseRepository

__all__ = ["MAX_SINK_ATTEMPTS", "GraphStoreSink", "SinkError", "SinkWorker", "TripleStoreSink"]

_log = logging.getLogger(__name__)

MAX_SINK_ATTEMPTS = 10
ERROR_MAX = 500
_TRANSIENT_STATUS = frozenset({408, 425, 429})


class SinkError(Exception):
    """A push to the triple store failed; ``transient`` says whether a retry may succeed."""

    def __init__(self, transient: bool, message: str) -> None:
        super().__init__(message)
        self.transient = transient
        self.message = message


class TripleStoreSink(Protocol):
    async def replace_graph(self, graph_iri: str, turtle: bytes) -> None: ...


class GraphStoreSink:
    """SPARQL 1.1 Graph Store HTTP Protocol client (``PUT`` replaces the named graph)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        username: str | None,
        password: SecretStr | None,
    ) -> None:
        self._http = http
        self._url = url
        self._auth = httpx.BasicAuth(username, _secret(password)) if username else None
        self._secrets = [s for s in (_secret(password),) if s]

    def _clean(self, text: str) -> str:
        return redact(text, self._secrets)[:ERROR_MAX]

    async def replace_graph(self, graph_iri: str, turtle: bytes) -> None:
        try:
            response = await self._http.put(
                self._url,
                params={"graph": graph_iri},
                content=turtle,
                headers={"Content-Type": "text/turtle"},
                auth=self._auth if self._auth is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as exc:  # timeouts and transport errors
            raise SinkError(
                True, self._clean(f"triple store unreachable ({type(exc).__name__}: {exc})")
            ) from None
        code = response.status_code
        if 200 <= code < 300:
            return
        transient = code >= 500 or code in _TRANSIENT_STATUS
        detail = self._clean(response.text.strip().replace("\n", " ")[:200])
        raise SinkError(
            transient, f"triple store answered HTTP {code}" + (f": {detail}" if detail else "")
        )


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def _join(base: str, tail: str) -> str:
    return (base if base.endswith(("/", "#")) else base + "/") + tail


class SinkWorker:
    """Drains the sink queue of ``releases``; runs in the server's background worker."""

    def __init__(
        self,
        settings: Settings,
        releases: ReleaseRepository,
        reader: ReleaseReader,
        models: ModelService,
        sink: TripleStoreSink,
        policies: Sequence[ModelPolicyEntry],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._releases = releases
        self._reader = reader
        self._models = models
        self._sink = sink
        self._policies = policies
        self._clock = clock
        remote = settings.git_remote or ""
        self.repo_key = hashlib.sha256(remote.encode()).hexdigest()
        self._lock = asyncio.Lock()  # the inline attempt and the loop never push one row twice

    async def process(self, now: datetime | None = None) -> list[Release]:
        """Attempt every due release; returns the rows attempted. Never raises."""
        at = now or self._clock()
        out: list[Release] = []
        try:
            if not self._settings.triplestore_enabled:
                return out
            for rel in await self._releases.sink_due(self.repo_key, at):
                done = await self._attempt_locked(rel.id, at)
                if done is not None:
                    out.append(done)
        except Exception as exc:  # recorded per row; a store failure is retried next call
            _log.warning("triple store worker failed (%s)", type(exc).__name__)
        return out

    async def attempt(self, release_id: str) -> Release | None:
        """The inline attempt right after a release. Never raises."""
        try:
            return await self._attempt_locked(release_id, self._clock())
        except Exception as exc:
            _log.warning("triple store attempt failed (%s)", type(exc).__name__)
            return None

    async def _attempt_locked(self, release_id: str, now: datetime) -> Release | None:
        async with self._lock:
            rel = await self._releases.get(release_id)
            if rel is None or rel.sink_status != "pending":
                return None
            return await self._attempt(rel, now)

    async def _attempt(self, rel: Release, now: datetime) -> Release:
        try:
            if not triplestore_for(self._policies, rel.model).enabled:
                rel = rel.model_copy(update={"sink_status": "none", "sink_next_attempt_at": None})
            else:
                await self._push(rel)
                rel = rel.model_copy(
                    update={
                        "sink_status": "done",
                        "sink_attempts": rel.sink_attempts + 1,
                        "sink_next_attempt_at": None,
                        "sink_last_error": None,
                    }
                )
        except SinkError as exc:
            rel = self._failed(rel, now, exc.transient, exc.message)
        except (LakeloomError, GitError) as exc:
            # The tag or the model is not readable right now; the next attempt may succeed.
            rel = self._failed(rel, now, True, redact(f"cannot read the release: {exc}"))
        except Exception as exc:
            rel = self._failed(rel, now, True, f"unexpected error ({type(exc).__name__})")
        try:
            await self._releases.update(rel)
        except Exception as exc:
            _log.warning(
                "release %s: could not record the sink state (%s)", rel.id, type(exc).__name__
            )
        return rel

    def _failed(self, rel: Release, now: datetime, transient: bool, message: str) -> Release:
        attempts = rel.sink_attempts + 1
        retry = transient and attempts < MAX_SINK_ATTEMPTS
        _log.warning(
            "release %s: triple store attempt %d failed (%s): %s",
            rel.id, attempts, "retry" if retry else "failed", message,
        )  # fmt: skip
        return rel.model_copy(
            update={
                "sink_status": "pending" if retry else "failed",
                "sink_attempts": attempts,
                "sink_next_attempt_at": (
                    now + timedelta(seconds=backoff_seconds(attempts)) if retry else None
                ),
                "sink_last_error": message[:ERROR_MAX],
            }
        )

    async def _push(self, rel: Release) -> None:
        model = await self._models.get_model(rel.model)
        if not model.base_iri:
            raise SinkError(False, f"model {rel.model!r} has no base IRI")
        base = model.base_iri
        # The reader may fetch from the remote under the repository lock: no lock held here.
        highest = (await self._reader.list(rel.model))[:1]
        ontology = await self._reader.file(rel.model, rel.version, "ontology.ttl")
        await self._sink.replace_graph(_join(base, f"release/{rel.version}"), ontology)
        if highest and highest[0].version == rel.version:
            await self._sink.replace_graph(_join(base, "latest"), ontology)
        shapes_on = triplestore_for(self._policies, rel.model).shapes
        if shapes_on is None:
            shapes_on = self._settings.triplestore_shapes
        if shapes_on:
            try:
                shapes = await self._reader.file(rel.model, rel.version, "shapes.ttl")
            except LakeloomError as exc:
                if exc.code != "not_found":
                    raise
                return
            await self._sink.replace_graph(_join(base, "shapes"), shapes)

    async def status(self) -> dict[str, Any]:
        """For ``server_info``: counts and the last error. Never raises, no URL, no password."""
        out: dict[str, Any] = {
            "enabled": self._settings.triplestore_enabled,
            "pending": 0,
            "failed": 0,
            "done": 0,
            "last_error": None,
        }
        if not self._settings.triplestore_enabled:
            return out
        try:
            counts = await self._releases.sink_counts(self.repo_key)
            out.update(pending=counts["pending"], failed=counts["failed"], done=counts["done"])
            out["last_error"] = await self._releases.sink_last_error(self.repo_key)
        except Exception as exc:
            out["last_error"] = f"release store unavailable ({type(exc).__name__})"
        return out
