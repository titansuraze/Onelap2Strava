"""End-to-end sync orchestration: Onelap -> fix -> Strava.

This module is the business glue, deliberately thin so that the two
heavy parts (Onelap client, Strava client) can be swapped / mocked
without touching sync logic.

Flow per activity:

1. Download raw FIT from Onelap to ``data/cache/`` (idempotent; skips if
   already present).
2. Run Phase 1's ``fix_fit`` to produce a WGS-84 FIT in ``data/output/``.
3. Use Phase 1's ``upload_fit`` to post to Strava (with its own dedup).

Uploading is decoupled from downloading: if the Strava step fails for
one activity we still attempt later activities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from stravalib.client import Client as StravaClient

from .fit_fixer import FixResult, fix_fit
from .onelap import Activity, OnelapClient
from .onelap.client import DownloadedFit
from .strava_auth import get_authenticated_client
from .strava_client import UploadOutcome, upload_fit

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/cache")


@dataclass
class ActivitySyncResult:
    """Outcome of syncing ONE Onelap activity end-to-end."""

    activity: Activity
    downloaded: DownloadedFit | None = None
    fixed: FixResult | None = None
    uploaded: UploadOutcome | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.uploaded is not None

    def pretty(self) -> str:
        if self.error:
            return f"[fail] {self.activity.short_description()}: {self.error}"
        if self.uploaded is None:
            return f"[skip] {self.activity.short_description()}: not uploaded"
        return f"[ok]   {self.activity.short_description()} -> {self.uploaded.pretty()}"


@dataclass
class SyncReport:
    """Aggregated result across all activities in one ``run_sync`` call."""

    results: list[ActivitySyncResult] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    @property
    def skipped_duplicate_count(self) -> int:
        return sum(
            1
            for r in self.results
            if r.uploaded is not None and r.uploaded.skipped_duplicate
        )


def _sync_one(
    onelap: OnelapClient,
    strava: StravaClient,
    activity: Activity,
    *,
    cache_dir: Path,
    force: bool,
    name: str | None,
) -> ActivitySyncResult:
    """Run the full pipeline on a single activity, catching per-activity errors."""
    result = ActivitySyncResult(activity=activity)
    try:
        downloaded = onelap.download_fit(activity, cache_dir=cache_dir)
        result.downloaded = downloaded
        logger.info("downloaded %s (%d bytes)", downloaded.path, downloaded.size_bytes)

        fixed = fix_fit(downloaded.path)
        result.fixed = fixed
        if fixed.start_time_utc is None:
            result.error = "could not determine start time from fit"
            return result
        logger.info(
            "fixed %s (%d points) start=%s",
            downloaded.filename,
            fixed.record_points_converted,
            fixed.start_time_utc.isoformat(),
        )

        uploaded = upload_fit(
            strava,
            fixed.output_path,
            start_time_utc=fixed.start_time_utc,
            name=name,
            force=force,
        )
        result.uploaded = uploaded
    except Exception as e:  # noqa: BLE001 - per-activity isolation is desired
        logger.exception("sync failed for activity %s", activity.activity_id)
        result.error = str(e)
    return result


def run_sync(
    *,
    limit: int = 1,
    force: bool = False,
    name: str | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    onelap: OnelapClient | None = None,
    strava: StravaClient | None = None,
) -> SyncReport:
    """Pull latest ``limit`` activities from Onelap, fix + upload each to Strava.

    Parameters are injectable primarily for tests: production callers
    pass nothing and the authenticated clients are constructed from disk.
    """
    from .onelap.auth import get_authenticated_onelap_client

    onelap = onelap or get_authenticated_onelap_client()
    strava = strava or get_authenticated_client()

    activities = onelap.list_activities(limit=limit)
    logger.info("pulled %d activities from Onelap (limit=%d)", len(activities), limit)

    report = SyncReport()
    for activity in activities:
        result = _sync_one(
            onelap,
            strava,
            activity,
            cache_dir=cache_dir,
            force=force,
            name=name,
        )
        report.results.append(result)
    return report
