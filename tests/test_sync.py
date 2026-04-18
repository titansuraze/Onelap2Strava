"""End-to-end mock test for the sync pipeline.

Exercises ``run_sync`` without touching the network:

- Fake Onelap client returns one activity and "downloads" a real fit
  fixture by copying it into the cache dir.
- Fake Strava client records the upload and reports no duplicates.
- A tmpfile-backed :class:`SyncLog` is injected per test so runs stay
  isolated from each other and from the user's real ``data/.sync.db``.

Covers:

- Happy path wiring.
- Strava-side ±10min dedup path.
- Per-activity isolation on download failure.
- Phase 3.1 fuzzy dedup (local log hit skips fix + upload entirely).
- Phase 3.1 retry policy (transient → success after retry, unrecoverable
  → exhausted).
- Phase 3.1 ``--incremental`` filtering.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from onelap2strava.onelap.client import DownloadedFit
from onelap2strava.onelap.models import Activity
from onelap2strava.sync import run_sync
from onelap2strava.sync_log import STATUS_DUPLICATE, STATUS_FAILED, STATUS_OK, SyncLog

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "test_data"
BIAS_FIT = FIXTURE_DIR / "MAGENE_C506_bias.fit"

pytestmark = pytest.mark.skipif(
    not BIAS_FIT.exists(),
    reason=(
        "test_data/MAGENE_C506_bias.fit missing; see test_data/README.md. "
        "Sync pipeline test needs a real fit to fix + upload."
    ),
)


class _FakeOnelap:
    """Drop-in stand-in for OnelapClient; no HTTP."""

    def __init__(self, activities: list[Activity], source_fit: Path) -> None:
        self.activities = activities
        self.source_fit = source_fit
        self.download_calls: list[Activity] = []

    def list_activities(self, *, limit: int | None = None) -> list[Activity]:
        out = list(self.activities)
        if limit is not None:
            out = out[:limit]
        return out

    def download_fit(self, activity: Activity, cache_dir: Path) -> DownloadedFit:
        self.download_calls.append(activity)
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest = cache_dir / (activity.filename_hint or f"{activity.activity_id}.fit")
        shutil.copyfile(self.source_fit, dest)
        return DownloadedFit(
            path=dest, filename=dest.name, size_bytes=dest.stat().st_size
        )


def _fake_activity(
    activity_id: str = "sync-test-1",
    created_at: int = 1_712_500_000,
    filename: str = "MAGENE_C506_sync_test.fit",
) -> Activity:
    return Activity.from_api(
        {
            "id": activity_id,
            "created_at": created_at,
            "totalDistance": 32_170,
            "elevation": 150,
            "durl": f"/analysis/download/{activity_id}.fit",
            "fileKey": filename,
        }
    )


def _strava_client_no_duplicates() -> MagicMock:
    """Mimic stravalib.Client with no existing activities + successful upload."""
    client = MagicMock()
    client.get_activities.return_value = iter([])

    fake_activity = MagicMock()
    fake_activity.id = 999_888
    uploader = MagicMock()
    uploader.wait.return_value = fake_activity
    client.upload_activity.return_value = uploader
    return client


@pytest.fixture
def sync_log(tmp_path: Path):
    """One fresh on-disk SQLite per test."""
    with SyncLog.open(tmp_path / ".sync.db") as log:
        yield log


def _no_sleep(_: float) -> None:
    """Retry backoff injection that runs instantly."""


# ---------- happy paths ----------


def test_run_sync_happy_path(tmp_path: Path, sync_log: SyncLog) -> None:
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)
    strava = _strava_client_no_duplicates()

    report = run_sync(
        limit=1,
        force=False,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.success_count == 1
    assert report.failure_count == 0
    result = report.results[0]
    assert result.ok
    assert result.downloaded is not None
    assert result.downloaded.path.exists()
    assert result.fixed is not None
    assert result.fixed.output_path.exists()
    assert result.fixed.start_time_utc is not None

    args, kwargs = strava.upload_activity.call_args
    assert kwargs["data_type"] == "fit"
    file_arg = kwargs["activity_file"]
    assert str(result.fixed.output_path) == file_arg.name

    assert result.uploaded is not None
    assert result.uploaded.new_activity_id == 999_888
    assert not result.uploaded.skipped_duplicate

    # Phase 3.1: the sync log now carries a row for this activity.
    rows = sync_log.recent()
    assert len(rows) == 1
    assert rows[0].onelap_activity_id == "sync-test-1"
    assert rows[0].status == STATUS_OK
    assert rows[0].strava_activity_id == 999_888


def test_run_sync_skips_when_strava_duplicate_found(
    tmp_path: Path, sync_log: SyncLog
) -> None:
    """Strava ±10min dedup path still engages when local log is empty."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)

    strava = MagicMock()
    existing = MagicMock()
    existing.id = 111_222
    existing.name = "Duplicate Ride"
    strava.get_activities.return_value = iter([existing])

    report = run_sync(
        limit=1,
        force=False,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.success_count == 1
    assert report.skipped_duplicate_count == 1
    strava.upload_activity.assert_not_called()
    rows = sync_log.recent()
    assert rows[0].status == STATUS_DUPLICATE
    assert rows[0].strava_activity_id == 111_222


def test_run_sync_per_activity_isolation(tmp_path: Path, sync_log: SyncLog) -> None:
    """If one activity fails, later ones should still be attempted."""
    good = _fake_activity()
    bad = Activity.from_api(
        {
            "id": "bad",
            "created_at": 1_712_400_000,
            "totalDistance": 1,
            "elevation": 0,
            "durl": "/analysis/download/missing.fit",
            "fileKey": "missing.fit",
        }
    )

    class _FlakyOnelap(_FakeOnelap):
        def download_fit(self, activity: Activity, cache_dir: Path) -> DownloadedFit:
            if activity.activity_id == "bad":
                raise RuntimeError("simulated download failure")
            return super().download_fit(activity, cache_dir)

    onelap = _FlakyOnelap([good, bad], source_fit=BIAS_FIT)
    strava = _strava_client_no_duplicates()

    report = run_sync(
        limit=5,
        force=False,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert len(report.results) == 2
    assert report.success_count == 1
    assert report.failure_count == 1
    err = next(r for r in report.results if r.error is not None)
    assert "simulated download failure" in err.error
    # Sync log carries both outcomes.
    statuses = {r.status for r in sync_log.recent()}
    assert statuses == {STATUS_OK, STATUS_FAILED}


# ---------- Phase 3.1: fuzzy dedup ----------


def test_fuzzy_hit_skips_fix_and_upload(tmp_path: Path, sync_log: SyncLog) -> None:
    """Second sync of the same ride never touches Strava."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)
    strava = _strava_client_no_duplicates()

    # First run populates the log.
    run_sync(
        limit=1,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )
    strava.reset_mock()

    # Second run: a new Onelap activity id but SAME start_time/duration/point
    # (we reuse the same fit bytes). Fuzzy match should fire.
    second = _fake_activity(
        activity_id="sync-test-2", filename="MAGENE_C506_rerun.fit"
    )
    onelap2 = _FakeOnelap([second], source_fit=BIAS_FIT)
    report = run_sync(
        limit=1,
        cache_dir=tmp_path / "cache",
        onelap=onelap2,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.skipped_duplicate_count == 1
    strava.get_activities.assert_not_called()
    strava.upload_activity.assert_not_called()
    assert any(r.onelap_activity_id == "sync-test-2" for r in sync_log.recent())


def test_force_bypasses_fuzzy_hit(tmp_path: Path, sync_log: SyncLog) -> None:
    """--force disables local fuzzy dedup and we go to Strava as before."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)
    strava = _strava_client_no_duplicates()
    run_sync(
        limit=1,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )
    strava.reset_mock()
    strava.get_activities.return_value = iter([])  # fresh iterator

    second = _fake_activity(
        activity_id="sync-test-2", filename="MAGENE_C506_rerun.fit"
    )
    onelap2 = _FakeOnelap([second], source_fit=BIAS_FIT)
    report = run_sync(
        limit=1,
        force=True,
        cache_dir=tmp_path / "cache",
        onelap=onelap2,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.success_count == 1
    assert report.skipped_duplicate_count == 0
    strava.upload_activity.assert_called_once()


# ---------- Phase 3.1: retry ----------


def test_retry_succeeds_after_transient_errors(tmp_path: Path, sync_log: SyncLog) -> None:
    """Two ConnectionErrors then success — result should be ok."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)

    strava = MagicMock()
    strava.get_activities.return_value = iter([])

    call_counter = {"n": 0}

    def flaky_upload(*args, **kwargs):
        call_counter["n"] += 1
        if call_counter["n"] < 3:
            raise requests.ConnectionError("flaky network")
        uploader = MagicMock()
        result = MagicMock()
        result.id = 42
        uploader.wait.return_value = result
        return uploader

    strava.upload_activity.side_effect = flaky_upload

    report = run_sync(
        limit=1,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.success_count == 1
    assert call_counter["n"] == 3
    rows = sync_log.recent()
    assert rows[0].status == STATUS_OK
    assert rows[0].strava_activity_id == 42


def test_retry_exhausts_and_fails(tmp_path: Path, sync_log: SyncLog) -> None:
    """All 3 attempts raise a retryable exception — row recorded as failed."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)

    strava = MagicMock()
    strava.get_activities.return_value = iter([])

    call_counter = {"n": 0}

    def always_fail(*args, **kwargs):
        call_counter["n"] += 1
        raise requests.ConnectionError("dead network")

    strava.upload_activity.side_effect = always_fail

    report = run_sync(
        limit=1,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.failure_count == 1
    assert call_counter["n"] == 3  # retried max_attempts times
    rows = sync_log.recent()
    assert rows[0].status == STATUS_FAILED


def test_non_retryable_error_fails_immediately(
    tmp_path: Path, sync_log: SyncLog
) -> None:
    """A ValueError (not a transient network error) should NOT be retried."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)

    strava = MagicMock()
    strava.get_activities.return_value = iter([])

    call_counter = {"n": 0}

    def bad_request(*args, **kwargs):
        call_counter["n"] += 1
        raise ValueError("malformed input")

    strava.upload_activity.side_effect = bad_request

    report = run_sync(
        limit=1,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    assert report.failure_count == 1
    assert call_counter["n"] == 1  # no retries


# ---------- Phase 3.1: incremental ----------


def test_incremental_skips_already_seen_activities(
    tmp_path: Path, sync_log: SyncLog
) -> None:
    """Second incremental sync with the same Onelap id returns an empty report."""
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)
    strava = _strava_client_no_duplicates()

    run_sync(
        incremental=True,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )
    strava.reset_mock()

    # Re-run: same Onelap activity id, so it should be filtered out entirely.
    report = run_sync(
        incremental=True,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )
    assert report.results == []
    strava.upload_activity.assert_not_called()
    strava.get_activities.assert_not_called()


def test_incremental_filters_out_seen_activities(
    tmp_path: Path, sync_log: SyncLog
) -> None:
    """Incremental run drops activity ids the sync log already knows.

    Shares a fixture across A1/A2/A3 so fuzzy dedup will also fire for
    any id that makes it past the incremental filter — that is fine;
    the behaviour under test is "id filtering happens before the
    per-activity pipeline", which we prove by counting ``report.results``.
    """
    a1 = _fake_activity(activity_id="A1", filename="A1.fit", created_at=1_712_400_000)
    a2 = _fake_activity(activity_id="A2", filename="A2.fit", created_at=1_712_500_000)
    strava = _strava_client_no_duplicates()

    first = run_sync(
        incremental=True,
        cache_dir=tmp_path / "cache",
        onelap=_FakeOnelap([a1, a2], source_fit=BIAS_FIT),
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )
    # Two activities surveyed even though only one reaches Strava (A2
    # fuzzy-dups A1 because they share the fixture). What we care about
    # is that the incremental filter did NOT drop either of these on the
    # first run — neither was seen before.
    assert len(first.results) == 2
    strava.reset_mock()
    strava.get_activities.return_value = iter([])

    a3 = _fake_activity(activity_id="A3", filename="A3.fit", created_at=1_712_600_000)
    report = run_sync(
        incremental=True,
        cache_dir=tmp_path / "cache",
        onelap=_FakeOnelap([a1, a2, a3], source_fit=BIAS_FIT),
        strava=strava,
        sync_log=sync_log,
        sleep=_no_sleep,
    )

    # Only A3 passes the incremental filter (A1/A2 are in seen_onelap_ids).
    # A3 then fuzzy-dups against the stored rows — also fine.
    assert len(report.results) == 1
    assert report.results[0].activity.activity_id == "A3"


# ---------- Phase 3.1: backfill auto-trigger ----------


def test_first_run_backfills_existing_cache(tmp_path: Path) -> None:
    """A pre-existing cache seeds the log before the first sync runs."""
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copyfile(BIAS_FIT, cache / "preexisting.fit")

    db_path = tmp_path / ".sync.db"
    onelap = _FakeOnelap([], source_fit=BIAS_FIT)  # nothing new on Onelap
    strava = _strava_client_no_duplicates()

    run_sync(
        limit=1,
        cache_dir=cache,
        onelap=onelap,
        strava=strava,
        db_path=db_path,
        sleep=_no_sleep,
    )

    with SyncLog.open(db_path) as log:
        assert log.count() == 1
        assert log.recent()[0].onelap_activity_id.startswith("backfilled:")
