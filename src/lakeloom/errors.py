"""Typed errors shared by every tool (FR-AGT-05).

Every failure an agent can see has a stable ``code``, a human ``message`` and a ``hint``
telling the caller what to do next.
"""

from typing import Any, Literal

__all__ = ["ErrorCode", "LakeloomError", "RateLimited"]

ErrorCode = Literal[
    "not_found",
    "forbidden",
    "conflict",
    "validation_error",
    "upstream_unavailable",
    "rate_limited",
]


class LakeloomError(Exception):
    """A typed, agent-readable error: ``{"error": {"code", "message", "hint"}}``."""

    def __init__(self, code: ErrorCode, message: str, hint: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.hint = hint

    def details(self) -> dict[str, Any]:
        """Extra structured fields returned next to ``error`` (JSON values); none by default."""
        return {}

    def to_dict(self) -> dict[str, dict[str, str]]:
        """The structured-content shape returned to MCP clients."""
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}


class RateLimited(LakeloomError):
    """The principal is over its rate limit (NFR-SEC-16); ``retry_after`` is whole seconds >= 1."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "rate_limited",
            "rate limit exceeded for this principal",
            f"retry after {retry_after} s",
        )
        self.retry_after = retry_after

    def details(self) -> dict[str, Any]:
        return {"retry_after": self.retry_after}
