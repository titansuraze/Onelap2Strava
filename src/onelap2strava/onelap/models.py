"""Typed representation of the Onelap activity list response.

Keeping this separate from the HTTP client lets the rest of the code
depend on a stable shape even if the raw API payload gains/renames
fields. All field mapping from JSON happens here, in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


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
        created_at_utc = cls._parse_created_at(item)

        durl = item.get("durl") or item.get("fitUrl") or ""
        if not durl and item.get("fileKey"):
            # 仅 OTM / fit_content 下载、无直链时占位（真实 URL 由 client 候选生成）
            durl = "https://u.onelap.cn/api/otm/ride_record/pending-filekey"
        if not durl and (item.get("id") or item.get("_id") or item.get("activity_id")):
            # ``/ride_record/list`` 常只返回摘要（无 durl / fileKey）；占位后由 client
            # 或详情接口补全。
            durl = "https://u.onelap.cn/api/otm/ride_record/pending-list-summary"
        if not durl:
            raise ValueError(f"activity has no download url: {item!r}")

        filename_hint = item.get("fileKey") or item.get("fitUrl")

        dist = item.get("totalDistance")
        if dist is None and item.get("distance_km") is not None:
            dist = float(item.get("distance_km") or 0) * 1000.0
        if dist is None:
            dist = 0.0
        elev = item.get("elevation")
        if elev is None:
            elev = item.get("elevation_m", 0)

        raw = dict(item)
        if raw.get("_id") is None and raw.get("id") is not None:
            raw["_id"] = str(raw["id"])

        return cls(
            activity_id=str(item.get("id") or item.get("activity_id") or durl),
            created_at_utc=created_at_utc,
            distance_m=float(dist or 0),
            elevation_m=float(elev or 0),
            download_path=str(durl),
            filename_hint=str(filename_hint) if filename_hint else None,
            raw=raw,
        )

    @staticmethod
    def _parse_created_at(item: dict[str, Any]) -> datetime:
        def _from_shanghai_string(s: str) -> datetime | None:
            s2 = s.strip()
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                if len(s2) < 10:
                    break
                try:
                    naive = datetime.strptime(s2[:19], fmt)
                    return naive.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(
                        timezone.utc
                    )
                except ValueError:
                    continue
            return None

        def _is_bogus_epoch(dt: datetime) -> bool:
            return dt.astimezone(timezone.utc).year < 2000

        created = item.get("created_at")
        if isinstance(created, (int, float)) and int(created) > 946_684_800:
            return datetime.fromtimestamp(int(created), tz=timezone.utc)
        if isinstance(created, str) and created.strip():
            s = created.strip()
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt_utc = dt.astimezone(timezone.utc)
                if not _is_bogus_epoch(dt_utc):
                    return dt_utc
            except ValueError:
                pass
        # 新接口常见：``"date": "2026-04-23 23:26"``（东八区本地时间）
        ds = item.get("date")
        if isinstance(ds, str) and ds.strip():
            dt2 = _from_shanghai_string(ds)
            if dt2 is not None:
                return dt2
        # 列表摘要常用 ``start_riding_time`` 替代占位 ``created_at``（如 1970-01-01）
        for key in ("start_riding_time", "startRidingTime", "ride_time", "rideTime"):
            st = item.get(key)
            if isinstance(st, str) and st.strip():
                dt2 = _from_shanghai_string(st)
                if dt2 is not None:
                    return dt2
        raise ValueError(f"activity missing usable created_at/date: {item!r}")

    def short_description(self) -> str:
        """Human-readable one-liner for CLI output."""
        km = self.distance_m / 1000.0
        return (
            f"{self.created_at_utc.astimezone().strftime('%Y-%m-%d %H:%M')} "
            f"distance={km:.1f}km elev={self.elevation_m:.0f}m "
            f"id={self.activity_id}"
        )
