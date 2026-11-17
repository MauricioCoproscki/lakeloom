"""Snowflake connector over ``INFORMATION_SCHEMA`` and ``SHOW … KEYS`` (FR-SRC-04).

``snowflake-connector-python`` is the optional ``snowflake`` extra and is imported lazily,
so the base install never loads it.
"""

import importlib.util

from lakeloom.config import Settings
from lakeloom.errors import LakeloomError
from lakeloom.sources.config import SnowflakeSourceConfig

__all__ = ["SnowflakeSource", "build_source"]


def _require_connector() -> None:
    if importlib.util.find_spec("snowflake") is None or (
        importlib.util.find_spec("snowflake.connector") is None
    ):
        raise LakeloomError(
            "validation_error",
            "the Snowflake connector needs the 'snowflake-connector-python' package",
            hint="pip install 'lakeloom[snowflake]'",
        )


class SnowflakeSource:
    """A read-only view of one Snowflake account."""

    kind = "snowflake"

    def __init__(self, config: SnowflakeSourceConfig) -> None:
        raise NotImplementedError


def build_source(config: SnowflakeSourceConfig, settings: Settings) -> SnowflakeSource:
    """Registry factory (FR-SRC-06)."""
    _require_connector()
    return SnowflakeSource(config)
