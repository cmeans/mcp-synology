"""What the client caches from SYNO.API.Info. Nothing here is written to disk."""

from __future__ import annotations

from pydantic import BaseModel


class ApiInfoEntry(BaseModel):
    """Cached API endpoint info from SYNO.API.Info."""

    path: str
    min_version: int
    max_version: int
    request_format: str | None = None
