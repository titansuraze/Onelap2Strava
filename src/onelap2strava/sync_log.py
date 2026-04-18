"""Local SQLite log of synced activities.

Phase 3.1 introduces this module as the single source of truth for
"what have we already processed". It serves two related reads:

1. **Fuzzy duplicate detection** — before uploading a freshly downloaded
   Onelap ride we query here by a ``(start_time, duration, start_point)``
   triple so re-exports of the same ride (slightly different bytes) do
   not reach Strava.
2. **Incremental sync** — ``sync --incremental`` asks this module for
   the set of already-seen Onelap activity ids and skips them when
   iterating the Onelap list response.

The schema is intentionally one flat table: making two (log + state)
would force us to decide where "failed" rows live and complicate the
fuzzy lookup. One table, filtered by ``status``, is simpler.

Design constraints inherited from earlier phases:

- **Zero new third-party deps**: ``sqlite3`` is in stdlib.
- **The log is a fast path, never the authority**: Strava's ``external_id``
  sha1 and its own ``get_activities ±10min`` window still back-stop.
  A missing/corrupt log degrades gracefully back to today's behaviour.
- **Backfill-aware**: first ``sync`` against an existing ``data/cache/``
  scans the cached fits and seeds the log, so fuzzy dedup works from
  turn one even for rides that predate this module.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .coords import haversine_m

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/.sync.db")

# Fuzzy dedup thresholds (see roadmap §97).
FUZZY_TIME_WINDOW = timedelta(minutes=10)
FUZZY_DURATION_RATIO = 0.05  # 5 %
FUZZY_START_DISTANCE_M = 500.0

# Status values recorded against an activity row.
STATUS_OK = "ok"
STATUS_DUPLICATE = "duplicate"
STATUS_FAILED = "failed"
STATUS_BACKFILLED = "backfilled"

# Rows that mean "this ride was already handled and should not be retried
# as a fresh upload". ``failed`` is excluded so a transient upload failure
# does not permanently mask the activity.
_HANDLED_STATUSES = (STATUS_OK, STATUS_DUPLICATE, STATUS_BACKFILLED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_activities (
    onelap_activity_id  TEXT PRIMARY KEY,
    fit_sha1            TEXT NOT NULL,
    start_time_utc      TEXT NOT NULL,
    duration_s          INTEGER,
    start_lat           REAL,
    start_lng           REAL,
    strava_activity_id  INTEGER,
    synced_at           TEXT NOT NULL,
    status              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_synced_activities_start_time
    ON synced_activities(start_time_utc);
"""


@dataclass
class SyncRecord:
    """One row of ``synced_activities`` as the rest of the code sees it."""

    onelap_activity_id: str
    fit_sha1: str
    start_time_utc: datetime
    duration_s: int | None
    start_lat: float | None
    start_lng: float | None
    strava_activity_id: int | None
    synced_at: datetime
    status: str

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "SyncRecord":
        return cls(
            onelap_activity_id=row["onelap_activity_id"],
            fit_sha1=row["fit_sha1"],
            start_time_utc=_parse_iso(row["start_time_utc"]),
            duration_s=row["duration_s"],
            start_lat=row["start_lat"],
            start_lng=row["start_lng"],
            strava_activity_id=row["strava_activity_id"],
            synced_at=_parse_iso(row["synced_at"]),
            status=row["status"],
        )


def _iso(dt: datetime) -> str:
    """Canonical UTC-ISO format. We store timezone-aware; assume naive=UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class SyncLog:
    """Thin wrapper over a SQLite connection used by :mod:`sync`.

    Use via :meth:`open` which returns a context manager so the underlying
    connection is always closed deterministically, or pass a pre-opened
    connection for test inject ability (``SyncLog(conn)``).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    @classmethod
    @contextmanager
    def open(cls, path: Path | str = DEFAULT_DB_PATH) -> Iterator["SyncLog"]:
        """Open (creating if needed) a SyncLog at ``path``.

        Using ``":memory:"`` as ``path`` gives a throwaway in-memory DB —
        used by tests.
        """
        path_obj = Path(path) if path != ":memory:" else None
        if path_obj is not None:
            path_obj.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            yield cls(conn)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def record_sync(
        self,
        *,
        onelap_activity_id: str,
        fit_sha1: str,
        start_time_utc: datetime,
        duration_s: int | None,
        start_lat: float | None,
        start_lng: float | None,
        strava_activity_id: int | None,
        status: str,
        synced_at: datetime | None = None,
    ) -> None:
        """Insert or replace one row.

        ``INSERT OR REPLACE`` means re-syncing the same Onelap id overwrites
        the previous row — useful when a retry turns a ``failed`` row into
        ``ok`` without leaving stale state behind.
        """
        synced_at = synced_at or datetime.now(tz=timezone.utc)
        self._conn.execute(
            """
            INSERT OR REPLACE INTO synced_activities (
                onelap_activity_id, fit_sha1, start_time_utc, duration_s,
                start_lat, start_lng, strava_activity_id, synced_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                onelap_activity_id,
                fit_sha1,
                _iso(start_time_utc),
                duration_s,
                start_lat,
                start_lng,
                strava_activity_id,
                _iso(synced_at),
                status,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Reads used by sync pipeline
    # ------------------------------------------------------------------ #

    def find_fuzzy_match(
        self,
        *,
        start_time_utc: datetime,
        duration_s: int | None,
        start_lat: float | None,
        start_lng: float | None,
    ) -> SyncRecord | None:
        """Return a previously-handled ride that matches the fuzzy triple.

        Matching criteria (all must hold when data is available):

        - Start time within ``FUZZY_TIME_WINDOW`` (hard requirement).
        - If both rows have ``duration_s``, the relative difference is
          below ``FUZZY_DURATION_RATIO``. If either is missing, this check
          is skipped (we do not gate the match on optional data).
        - If both rows have a ``start_lat/lng``, the great-circle distance
          is below ``FUZZY_START_DISTANCE_M``. Same fallback as above.

        The time window is applied via indexed SQL; duration and distance
        are filtered in Python because SQLite cannot compute haversine.
        """
        lo = _iso(start_time_utc - FUZZY_TIME_WINDOW)
        hi = _iso(start_time_utc + FUZZY_TIME_WINDOW)
        placeholders = ",".join("?" for _ in _HANDLED_STATUSES)
        rows = self._conn.execute(
            f"""
            SELECT * FROM synced_activities
            WHERE start_time_utc BETWEEN ? AND ?
              AND status IN ({placeholders})
            ORDER BY start_time_utc
            """,
            (lo, hi, *_HANDLED_STATUSES),
        ).fetchall()
        for row in rows:
            record = SyncRecord._from_row(row)
            if not _duration_close_enough(duration_s, record.duration_s):
                continue
            if not _start_point_close_enough(
                start_lat, start_lng, record.start_lat, record.start_lng
            ):
                continue
            return record
        return None

    def seen_onelap_ids(self) -> set[str]:
        """Onelap activity ids already handled (any non-failed status).

        ``backfilled`` placeholder ids (``backfilled:<filename>``) are
        excluded intentionally: they exist to power fuzzy dedup by
        start-time/point but they cannot match a real Onelap activity id
        returned by ``/analysis/list``. Keeping them in this set would do
        no harm today — but would silently stop matching if Onelap ever
        returned an id starting with ``backfilled:`` for some reason.
        """
        rows = self._conn.execute(
            """
            SELECT onelap_activity_id FROM synced_activities
            WHERE status IN (?, ?)
              AND onelap_activity_id NOT LIKE 'backfilled:%'
            """,
            (STATUS_OK, STATUS_DUPLICATE),
        ).fetchall()
        return {row["onelap_activity_id"] for row in rows}

    def recent(self, limit: int = 20) -> list[SyncRecord]:
        """Most recently synced rows for the ``sync-log`` CLI/debug command."""
        rows = self._conn.execute(
            "SELECT * FROM synced_activities ORDER BY synced_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [SyncRecord._from_row(r) for r in rows]

    def count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM synced_activities").fetchone()[
                "n"
            ]
        )

    # ------------------------------------------------------------------ #
    # Backfill (phase 3.1 bootstrap)
    # ------------------------------------------------------------------ #

    def backfill_from_cache(
        self,
        cache_dir: Path,
        *,
        read_metadata,  # callable(Path) -> FitMetadata, injected to avoid import cycle
    ) -> int:
        """Seed the log from existing cached fits.

        Idempotent: rows keyed by ``backfilled:<filename>`` so running
        this twice does not duplicate entries. Files that fail to parse
        are logged and skipped — backfill must never crash the app on a
        single bad file.

        Returns the number of rows actually inserted (not counting
        already-present placeholders). We detect "already present" by
        checking the primary key before the insert, so the caller can
        decide whether to announce the backfill.
        """
        if not cache_dir.exists():
            return 0

        inserted = 0
        now = datetime.now(tz=timezone.utc)
        for fit_path in sorted(cache_dir.glob("*.fit")):
            onelap_id = f"backfilled:{fit_path.name}"
            existing = self._conn.execute(
                "SELECT 1 FROM synced_activities WHERE onelap_activity_id = ?",
                (onelap_id,),
            ).fetchone()
            if existing is not None:
                continue
            try:
                meta = read_metadata(fit_path)
            except Exception as e:  # noqa: BLE001 - one bad fit should not abort
                logger.warning("backfill: skipping %s: %s", fit_path.name, e)
                continue
            if meta.start_time_utc is None:
                logger.warning(
                    "backfill: skipping %s (no start time)", fit_path.name
                )
                continue
            self.record_sync(
                onelap_activity_id=onelap_id,
                fit_sha1=meta.fit_sha1,
                start_time_utc=meta.start_time_utc,
                duration_s=meta.duration_s,
                start_lat=meta.start_lat,
                start_lng=meta.start_lng,
                strava_activity_id=None,
                status=STATUS_BACKFILLED,
                synced_at=now,
            )
            inserted += 1
        if inserted:
            logger.info("backfilled %d cached fits into sync log", inserted)
        return inserted


def _duration_close_enough(a: int | None, b: int | None) -> bool:
    """Fuzzy comparison that is lenient when either side is unknown."""
    if a is None or b is None:
        return True
    if a == 0 and b == 0:
        return True
    longer = max(abs(a), abs(b))
    if longer == 0:
        return True
    return abs(a - b) / longer < FUZZY_DURATION_RATIO


def _start_point_close_enough(
    lat1: float | None,
    lng1: float | None,
    lat2: float | None,
    lng2: float | None,
) -> bool:
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return True
    return haversine_m(lat1, lng1, lat2, lng2) < FUZZY_START_DISTANCE_M
