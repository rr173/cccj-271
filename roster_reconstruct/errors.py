"""Errors for the roster reconstruction engine.

Error objects intentionally carry structured, safe details.  They must never be
constructed with raw source records or word text.
"""

from __future__ import annotations

from typing import Any


class RosterError(Exception):
    """Base class for all deliberately reported failures."""

    code = "roster_error"

    def __init__(self, code: str | None = None, message: str | None = None, **details: Any) -> None:
        if code is not None:
            self.code = code
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": "error",
            "error_code": self.code,
            "message": self.message,
        }
        if self.details:
            data["details"] = _json_safe(self.details)
        return data


class PageDescriptionError(RosterError):
    """A malformed, illegal, or geometrically inconsistent page description."""

    code = "bad_page_description"


class DuplicateCellError(PageDescriptionError):
    code = "duplicate_logical_cell"


class StagingError(RosterError):
    code = "staging_error"


class ArchiveError(RosterError):
    """A rejected archive ingest or an invalid archive lookup request."""

    code = "archive_error"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)
