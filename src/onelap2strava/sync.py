"""End-to-end sync orchestration: Onelap -> fix -> Strava.

This module is the business glue, deliberately thin so the heavy parts
(Onelap client, Strava client, fit fixer) can be swapped or mocked
without touching sync logic.

Flow per activity:

1. Download raw FIT from Onelap to ``data/cache/`` (idempotent).
2. Read metadata from the raw fit (start time, duration, start point,
   sha1) and query the local :class:`SyncLog` for a fuzzy match. A hit
   records the outcome and returns — we never fix or upload something
   the log has already seen.
3. Run Phase 1's ``fix_fit`` to produce a WGS-84 FIT in ``data/output/``.
4. Use Phase 1's ``upload_fit`` to post to Strava (still with its own
   ±10min + sha1 dedup); wrapped in :func:`_with_retry` so transient
   network errors do not permanently fail the run.
5. Record the outcome in the sync log — success, duplicate (local
   fuzzy or Strava ±10min), or failure.

Per-activity isolation: if one activity fails the rest are still
attempted, same as before. The sync log captures failures too so they
can be retried on a later run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

import requests
from stravalib.client import Client as StravaClient

from .fit_fixer import FitMetadata, FixResult, fix_fit, read_fit_metadata
from .onelap import Activity, OnelapClient
from .onelap.client import DownloadedFit
from .strava_auth import get_authenticated_client
from .strava_client import UploadOutcome, upload_fit
from .sync_log import (
    DEFAULT_DB_PATH,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_OK,
    SyncLog,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data/cache")

T = TypeVar("T")

# Exceptions we classify as transient and worth retrying. We deliberately
# do NOT catch a broad ``Exception`` because (1) auth errors need to
# surface immediately with a clear prompt, and (2) fit-parse errors
# repeating will not fix themselves — retrying them just wastes time.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)

RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_BACKOFF_S = 1.0


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


def _with_retry(
    operation: Callable[[], T],
    *,
    description: str,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    base_backoff_s: float = RETRY_BASE_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``, retrying on transient exceptions only.

    Backoff is exponential: ``base_backoff_s * 2**(attempt-1)``. The
    default ``1s -> 2s -> 4s`` totals 7 s in the worst case, small
    enough not to frustrate a user waiting on the terminal and large
    enough to ride out most DNS blips and connection resets.

    ``sleep`` is injectable so tests can run instantly.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except RETRYABLE_EXCEPTIONS as e:
            last_exc = e
            if attempt == max_attempts:
                break
            wait_s = base_backoff_s * (2 ** (attempt - 1))
            logger.warning(
                "transient error during %s (attempt %d/%d): %s; retrying in %.1fs",
                description,
                attempt,
                max_attempts,
                e,
                wait_s,
            )
            sleep(wait_s)
    assert last_exc is not None  # loop ran at least once
    raise last_exc


def _fuzzy_hit_outcome(match) -> UploadOutcome:
    """Shape a sync log hit as an ``UploadOutcome`` the report can render."""
    existing_id = match.strava_activity_id
    return UploadOutcome(
        skipped_duplicate=True,
        existing_activity_id=existing_id,
        new_activity_id=None,
        activity_url=(
            f"https://www.strava.com/activities/{existing_id}"
            if existing_id is not None
            else None
        ),
        external_id=f"local-fuzzy:{match.fit_sha1[:12]}",
    )


def _sync_one(
    onelap: OnelapClient,
    strava: StravaClient,
    activity: Activity,
    *,
    cache_dir: Path,
    force: bool,
    name: str | None,
    sync_log: SyncLog,
    sleep: Callable[[float], None] = time.sleep,
) -> ActivitySyncResult:
    """Run the full pipeline on a single activity, catching per-activity errors.

    Every successful, duplicate, or failed path writes to ``sync_log``
    exactly once so the log is a faithful record of what the tool saw.
    """
    result = ActivitySyncResult(activity=activity)
    raw_meta: FitMetadata | None = None
    try:
        downloaded = onelap.download_fit(activity, cache_dir=cache_dir)
        result.downloaded = downloaded
        logger.info("downloaded %s (%d bytes)", downloaded.path, downloaded.size_bytes)

        raw_meta = read_fit_metadata(downloaded.path)

        # Fuzzy dedup BEFORE fix: a hit avoids the whole fix + upload +
        # Strava query path. Skipped when --force so users can re-drive
        # the pipeline deliberately.
        if not force and raw_meta.start_time_utc is not None:
            match = sync_log.find_fuzzy_match(
                start_time_utc=raw_meta.start_time_utc,
                duration_s=raw_meta.duration_s,
                start_lat=raw_meta.start_lat,
                start_lng=raw_meta.start_lng,
            )
            if match is not None:
                logger.info(
                    "fuzzy dedup: activity %s matches existing log row %s",
                    activity.activity_id,
                    match.onelap_activity_id,
                )
                result.uploaded = _fuzzy_hit_outcome(match)
                sync_log.record_sync(
                    onelap_activity_id=activity.activity_id,
                    fit_sha1=raw_meta.fit_sha1,
                    start_time_utc=raw_meta.start_time_utc,
                    duration_s=raw_meta.duration_s,
                    start_lat=raw_meta.start_lat,
                    start_lng=raw_meta.start_lng,
                    strava_activity_id=match.strava_activity_id,
                    status=STATUS_DUPLICATE,
                )
                return result

        fixed = fix_fit(downloaded.path)
        result.fixed = fixed
        if fixed.start_time_utc is None:
            result.error = "could not determine start time from fit"
            _record_failure(sync_log, activity, raw_meta, result.error)
            return result
        logger.info(
            "fixed %s (%d points) start=%s",
            downloaded.filename,
            fixed.record_points_converted,
            fixed.start_time_utc.isoformat(),
        )

        uploaded = _with_retry(
            lambda: upload_fit(
                strava,
                fixed.output_path,
                start_time_utc=fixed.start_time_utc,
                name=name,
                force=force,
            ),
            description=f"upload activity {activity.activity_id}",
            sleep=sleep,
        )
        result.uploaded = uploaded

        status = STATUS_DUPLICATE if uploaded.skipped_duplicate else STATUS_OK
        strava_id = uploaded.new_activity_id or uploaded.existing_activity_id
        sync_log.record_sync(
            onelap_activity_id=activity.activity_id,
            fit_sha1=raw_meta.fit_sha1,
            start_time_utc=(
                raw_meta.start_time_utc
                if raw_meta.start_time_utc is not None
                else fixed.start_time_utc
            ),
            duration_s=raw_meta.duration_s,
            start_lat=raw_meta.start_lat,
            start_lng=raw_meta.start_lng,
            strava_activity_id=strava_id,
            status=status,
        )
    except Exception as e:  # noqa: BLE001 - per-activity isolation is desired
        logger.exception("sync failed for activity %s", activity.activity_id)
        result.error = str(e)
        _record_failure(sync_log, activity, raw_meta, str(e))
    return result


def _record_failure(
    sync_log: SyncLog,
    activity: Activity,
    raw_meta: FitMetadata | None,
    _reason: str,
) -> None:
    """Write a ``failed`` row; defensive about missing metadata.

    Tolerates the case where we failed so early (e.g. during download)
    that we never parsed the fit. Uses the Onelap listing's
    ``created_at_utc`` as a fallback start time so the row is still
    useful for debugging.
    """
    try:
        sync_log.record_sync(
            onelap_activity_id=activity.activity_id,
            fit_sha1=raw_meta.fit_sha1 if raw_meta is not None else "",
            start_time_utc=(
                raw_meta.start_time_utc
                if raw_meta is not None and raw_meta.start_time_utc is not None
                else activity.created_at_utc
            ),
            duration_s=raw_meta.duration_s if raw_meta is not None else None,
            start_lat=raw_meta.start_lat if raw_meta is not None else None,
            start_lng=raw_meta.start_lng if raw_meta is not None else None,
            strava_activity_id=None,
            status=STATUS_FAILED,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "could not record failure for activity %s in sync log",
            activity.activity_id,
        )


def run_sync(
    *,
    limit: int = 1,
    force: bool = False,
    name: str | None = None,
    incremental: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    onelap: OnelapClient | None = None,
    strava: StravaClient | None = None,
    sync_log: SyncLog | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncReport:
    """Pull activities from Onelap, fix + upload each to Strava.

    Parameters
    ----------
    limit:
        When ``incremental`` is ``False`` (default), only the most recent
        ``limit`` activities are considered.
    incremental:
        When ``True``, pulls the full Onelap list and filters out
        activity ids already present in the sync log. ``limit`` is
        ignored in this mode.
    force:
        Skip local fuzzy dedup and Strava's ±10min window dedup. Mainly
        useful for re-driving a specific activity through the pipeline.
    sync_log:
        Pre-opened log for tests. Production callers leave this as None
        and pass ``db_path`` instead; the log is opened and closed as a
        context manager.
    sleep:
        Injection seam for test-time retry speed. Tests pass ``lambda _: None``.

    Parameters are injectable primarily for tests: production callers
    pass nothing and the authenticated clients are constructed from disk.
    """
    from .onelap.auth import get_authenticated_onelap_client

    onelap = onelap or get_authenticated_onelap_client()
    strava = strava or get_authenticated_client()

    if sync_log is not None:
        return _run_with_log(
            onelap,
            strava,
            sync_log,
            limit=limit,
            force=force,
            name=name,
            incremental=incremental,
            cache_dir=cache_dir,
            sleep=sleep,
        )
    with SyncLog.open(db_path) as log:
        return _run_with_log(
            onelap,
            strava,
            log,
            limit=limit,
            force=force,
            name=name,
            incremental=incremental,
            cache_dir=cache_dir,
            sleep=sleep,
        )


def _run_with_log(
    onelap: OnelapClient,
    strava: StravaClient,
    sync_log: SyncLog,
    *,
    limit: int,
    force: bool,
    name: str | None,
    incremental: bool,
    cache_dir: Path,
    sleep: Callable[[float], None],
) -> SyncReport:
    # First-run bootstrap: seed the log from any pre-existing cache. We
    # only do this on a truly empty log so we don't re-scan on every
    # run; the backfill itself is idempotent in case the check races.
    if sync_log.count() == 0:
        backfilled = sync_log.backfill_from_cache(
            cache_dir, read_metadata=read_fit_metadata
        )
        if backfilled:
            logger.info("backfilled %d cached fits into sync log", backfilled)

    if incremental:
        activities = onelap.list_activities()
        seen = sync_log.seen_onelap_ids()
        before = len(activities)
        activities = [a for a in activities if a.activity_id not in seen]
        logger.info(
            "incremental: %d new activity(ies) after filtering %d seen (of %d total)",
            len(activities),
            before - len(activities),
            before,
        )
    else:
        seen = sync_log.seen_onelap_ids()
        full = onelap.list_activities()
        before = len(full)
        full = [a for a in full if a.activity_id not in seen]
        activities = full[:limit]
        logger.info(
            "pulled %d activities from Onelap (after skipping %d seen, limit=%d)",
            len(activities),
            before - len(full),
            limit,
        )

    report = SyncReport()
    for activity in activities:
        result = _sync_one(
            onelap,
            strava,
            activity,
            cache_dir=cache_dir,
            force=force,
            name=name,
            sync_log=sync_log,
            sleep=sleep,
        )
        report.results.append(result)
    return report
