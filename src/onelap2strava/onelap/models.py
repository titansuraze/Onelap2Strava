"""Typed representation of the Onelap activity list response.

Keeping this separate from the HTTP client lets the rest of the code
depend on a stable shape even if the raw API payload gains/renames
fields. All field mapping from JSON happens here, in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass
class Activity:
    """One ride on Onelap.

    The fields here are the intersection of what we observed in practice
    and what the sync pipeline actually needs. Any extra keys from the
    raw payload are preserved under ``raw`` for debugging.
    """

    activity_id: str
    created_at_utc: datetime
    distance_m: float
    elevation_m: float
    download_path: str  # relative path on u.onelap.cn, e.g. "/analysis/download/XXX.fit"
    filename_hint: str | None
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "Activity":
        created = item.get("created_at")
        if isinstance(created, (int, float)):
            created_at_utc = datetime.fromtimestamp(int(created), tz=timezone.utc)
        elif isinstance(created, str):
            # Tolerate ISO strings just in case the API changes shape.
            created_at_utc = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if created_at_utc.tzinfo is None:
                created_at_utc = created_at_utc.replace(tzinfo=timezone.utc)
        else:
            raise ValueError(f"activity missing usable created_at: {item!r}")

        durl = item.get("durl") or item.get("fitUrl") or ""
        if not durl:
            raise ValueError(f"activity has no download url: {item!r}")

        filename_hint = item.get("fileKey") or item.get("fitUrl")

        return cls(
            activity_id=str(item.get("id") or item.get("activity_id") or durl),
            created_at_utc=created_at_utc,
            distance_m=float(item.get("totalDistance") or 0),
            elevation_m=float(item.get("elevation") or 0),
            download_path=str(durl),
            filename_hint=str(filename_hint) if filename_hint else None,
            raw=dict(item),
        )

    def short_description(self) -> str:
        """Human-readable one-liner for CLI output."""
        km = self.distance_m / 1000.0
        return (
            f"{self.created_at_utc.astimezone().strftime('%Y-%m-%d %H:%M')} "
            f"distance={km:.1f}km elev={self.elevation_m:.0f}m "
            f"id={self.activity_id}"
        )
