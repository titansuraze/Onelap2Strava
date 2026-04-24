"""Unit tests for the Phase 3.1 local sync log.

Tests run against an on-disk SQLite file under ``tmp_path`` (and one
in-memory case) so we exercise the same file-path code path production
uses. Haversine behaviour itself is already covered by ``test_coords``;
here we only verify that the log's fuzzy-match uses it sensibly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from onelap2strava.sync_log import (
    FUZZY_DURATION_RATIO,
    STATUS_BACKFILLED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_MANUAL,
    STATUS_OK,
    SyncLog,
)


def _utc(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


BASE_TIME = _utc(2025, 4, 1, 10, 0)


def _record(
    log: SyncLog,
    *,
    onelap_id: str = "abc",
    start: datetime = BASE_TIME,
    duration_s: int | None = 3600,
    start_lat: float | None = 39.9087,
    start_lng: float | None = 116.3975,
    strava_id: int | None = 999,
    status: str = STATUS_OK,
    fit_sha1: str = "deadbeef",
) -> None:
    log.record_sync(
        onelap_activity_id=onelap_id,
        fit_sha1=fit_sha1,
        start_time_utc=start,
        duration_s=duration_s,
        start_lat=start_lat,
        start_lng=start_lng,
        strava_activity_id=strava_id,
        status=status,
    )


# ---------- schema / idempotency ----------


def test_open_creates_db(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / ".sync.db"
    with SyncLog.open(db_path) as log:
        assert log.count() == 0
    assert db_path.exists()


def test_schema_is_idempotent(tmp_path: Path) -> None:
    """Opening twice must not error; existing rows survive."""
    db_path = tmp_path / ".sync.db"
    with SyncLog.open(db_path) as log:
        _record(log, onelap_id="keep-me")
    with SyncLog.open(db_path) as log:
        assert log.count() == 1


def test_record_replaces_on_same_id(tmp_path: Path) -> None:
    """A retry turning ``failed`` -> ``ok`` must overwrite, not append."""
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="same", status=STATUS_FAILED, strava_id=None)
        assert log.count() == 1
        _record(log, onelap_id="same", status=STATUS_OK, strava_id=123)
        assert log.count() == 1
        assert log.recent()[0].status == STATUS_OK
        assert log.recent()[0].strava_activity_id == 123


# ---------- fuzzy match: each axis in isolation ----------


def test_fuzzy_match_within_all_thresholds(tmp_path: Path) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="a", start=BASE_TIME, duration_s=3600)
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME + timedelta(minutes=5),
            duration_s=3620,  # <5% diff
            start_lat=39.9090,  # ~tens of meters away
            start_lng=116.3975,
        )
        assert match is not None
        assert match.onelap_activity_id == "a"


def test_fuzzy_match_rejects_outside_time_window(tmp_path: Path) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="a", start=BASE_TIME)
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME + timedelta(minutes=11),
            duration_s=3600,
            start_lat=39.9087,
            start_lng=116.3975,
        )
        assert match is None


def test_fuzzy_match_rejects_large_duration_gap(tmp_path: Path) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="a", start=BASE_TIME, duration_s=3600)
        # 20% longer — way outside FUZZY_DURATION_RATIO.
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME,
            duration_s=int(3600 * (1 + FUZZY_DURATION_RATIO + 0.05)),
            start_lat=39.9087,
            start_lng=116.3975,
        )
        assert match is None


def test_fuzzy_match_rejects_far_start_point(tmp_path: Path) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        # Beijing Tiananmen stored; probe point ~5 km south.
        _record(
            log,
            onelap_id="a",
            start=BASE_TIME,
            start_lat=39.9087,
            start_lng=116.3975,
        )
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME,
            duration_s=3600,
            start_lat=39.86,
            start_lng=116.3975,
        )
        assert match is None


def test_fuzzy_match_tolerates_missing_optional_fields(tmp_path: Path) -> None:
    """When duration or start point is unknown on either side, do not gate on it."""
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(
            log,
            onelap_id="a",
            start=BASE_TIME,
            duration_s=None,
            start_lat=None,
            start_lng=None,
        )
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME + timedelta(minutes=2),
            duration_s=3600,
            start_lat=39.9087,
            start_lng=116.3975,
        )
        assert match is not None


def test_fuzzy_match_excludes_failed_rows(tmp_path: Path) -> None:
    """A previous FAILED sync should not fake-dedup a new attempt."""
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="a", start=BASE_TIME, status=STATUS_FAILED)
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME,
            duration_s=3600,
            start_lat=39.9087,
            start_lng=116.3975,
        )
        assert match is None


def test_fuzzy_match_includes_backfilled(tmp_path: Path) -> None:
    """Backfilled rows must participate in fuzzy dedup — that's their point."""
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="backfilled:foo.fit", start=BASE_TIME, status=STATUS_BACKFILLED)
        match = log.find_fuzzy_match(
            start_time_utc=BASE_TIME,
            duration_s=3600,
            start_lat=39.9087,
            start_lng=116.3975,
        )
        assert match is not None
        assert match.status == STATUS_BACKFILLED


# ---------- seen ids (for --incremental) ----------


def test_seen_onelap_ids_excludes_backfilled_and_failed_includes_manual(
    tmp_path: Path,
) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        _record(log, onelap_id="real-1", status=STATUS_OK)
        _record(log, onelap_id="real-2", status=STATUS_DUPLICATE)
        _record(log, onelap_id="real-3", status=STATUS_FAILED)
        _record(log, onelap_id="real-4", status=STATUS_MANUAL)
        _record(log, onelap_id="backfilled:xx.fit", status=STATUS_BACKFILLED)
        assert log.seen_onelap_ids() == {"real-1", "real-2", "real-4"}


def test_mark_onelap_manual_inserts_row(tmp_path: Path) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        log.mark_onelap_manual("9")
    with SyncLog.open(tmp_path / "x.db") as log:
        assert "9" in log.seen_onelap_ids()
        assert log.recent()[0].status == STATUS_MANUAL


# ---------- backfill ----------


@dataclass
class _FakeMeta:
    fit_sha1: str = "aabb"
    start_time_utc: datetime | None = BASE_TIME
    duration_s: int | None = 3600
    start_lat: float | None = 39.9087
    start_lng: float | None = 116.3975


def test_backfill_from_empty_dir_returns_zero(tmp_path: Path) -> None:
    with SyncLog.open(tmp_path / "x.db") as log:
        assert log.backfill_from_cache(tmp_path / "missing", read_metadata=lambda p: _FakeMeta()) == 0
        empty = tmp_path / "empty"
        empty.mkdir()
        assert log.backfill_from_cache(empty, read_metadata=lambda p: _FakeMeta()) == 0


def test_backfill_reads_fits_and_is_idempotent(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.fit").write_bytes(b"\0")
    (cache / "b.fit").write_bytes(b"\0")
    (cache / "README.txt").write_text("not a fit")

    calls: list[Path] = []

    def read(p: Path) -> _FakeMeta:
        calls.append(p)
        return _FakeMeta()

    with SyncLog.open(tmp_path / "x.db") as log:
        assert log.backfill_from_cache(cache, read_metadata=read) == 2
        assert log.count() == 2
        # Second run: idempotent, metadata reader is only invoked for *new* keys.
        calls.clear()
        assert log.backfill_from_cache(cache, read_metadata=read) == 0
        assert calls == []


def test_backfill_skips_fits_without_start_time(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "broken.fit").write_bytes(b"\0")

    def read(p: Path) -> _FakeMeta:
        return _FakeMeta(start_time_utc=None)

    with SyncLog.open(tmp_path / "x.db") as log:
        assert log.backfill_from_cache(cache, read_metadata=read) == 0
        assert log.count() == 0


def test_backfill_tolerates_reader_exceptions(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "good.fit").write_bytes(b"\0")
    (cache / "bad.fit").write_bytes(b"\0")

    def read(p: Path) -> _FakeMeta:
        if p.name == "bad.fit":
            raise RuntimeError("nope")
        return _FakeMeta()

    with SyncLog.open(tmp_path / "x.db") as log:
        assert log.backfill_from_cache(cache, read_metadata=read) == 1
        ids = {r.onelap_activity_id for r in log.recent()}
        assert ids == {"backfilled:good.fit"}


# ---------- persistence smoke test ----------


def test_records_survive_close_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    with SyncLog.open(db_path) as log:
        _record(log, onelap_id="persist-1")
    with SyncLog.open(db_path) as log:
        assert log.count() == 1
        assert log.recent()[0].onelap_activity_id == "persist-1"


def test_in_memory_db(tmp_path: Path) -> None:
    """":memory:" support lets tests avoid disk when appropriate."""
    with SyncLog.open(":memory:") as log:
        _record(log, onelap_id="mem-1")
        assert log.count() == 1
