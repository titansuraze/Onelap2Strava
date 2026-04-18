"""End-to-end mock test for the sync pipeline.

Exercises ``run_sync`` without touching the network:

- Fake Onelap client returns one activity and "downloads" a real fit
  fixture by copying it into the cache dir.
- Fake Strava client records the upload and reports no duplicates.

The purpose is to verify the wiring: does download land in the cache,
does fix_fit get called, does the upload happen with the fixed file's
path and the correct start time?
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from onelap2strava.onelap.client import DownloadedFit
from onelap2strava.onelap.models import Activity
from onelap2strava.sync import run_sync

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


def _fake_activity() -> Activity:
    return Activity.from_api(
        {
            "id": "sync-test-1",
            "created_at": 1_712_500_000,
            "totalDistance": 32_170,
            "elevation": 150,
            "durl": "/analysis/download/sync-test-1.fit",
            "fileKey": "MAGENE_C506_sync_test.fit",
        }
    )


def _strava_client_no_duplicates() -> MagicMock:
    """Mimic stravalib.Client with no existing activities + successful upload."""
    client = MagicMock()
    # get_activities is used for time-window dedup; empty => no duplicate.
    client.get_activities.return_value = iter([])

    # upload_activity returns an "uploader" whose wait() yields an activity.
    fake_activity = MagicMock()
    fake_activity.id = 999_888
    uploader = MagicMock()
    uploader.wait.return_value = fake_activity
    client.upload_activity.return_value = uploader
    return client


def test_run_sync_happy_path(tmp_path: Path) -> None:
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)
    strava = _strava_client_no_duplicates()

    report = run_sync(
        limit=1,
        force=False,
        cache_dir=tmp_path / "cache",
        onelap=onelap,
        strava=strava,
    )

    assert report.success_count == 1
    assert report.failure_count == 0
    result = report.results[0]
    assert result.ok
    assert result.downloaded is not None
    assert result.downloaded.path.exists()
    # Fit was corrected into data/output/... (the default that fix_fit uses).
    assert result.fixed is not None
    assert result.fixed.output_path.exists()
    assert result.fixed.start_time_utc is not None
    # Strava got the fixed path, not the raw cache path.
    args, kwargs = strava.upload_activity.call_args
    assert kwargs["data_type"] == "fit"
    # The upload_fit impl opens the file and passes the handle; the handle's
    # name should be the fixed fit path.
    file_arg = kwargs["activity_file"]
    assert str(result.fixed.output_path) == file_arg.name
    # Upload outcome reports the new Strava activity id.
    assert result.uploaded is not None
    assert result.uploaded.new_activity_id == 999_888
    assert not result.uploaded.skipped_duplicate


def test_run_sync_skips_when_duplicate_found(tmp_path: Path) -> None:
    onelap = _FakeOnelap([_fake_activity()], source_fit=BIAS_FIT)

    # Strava returns an existing activity inside the +-10min window -> dedup.
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
    )

    assert report.success_count == 1  # "skipped duplicate" still counts as ok
    assert report.skipped_duplicate_count == 1
    strava.upload_activity.assert_not_called()


def test_run_sync_per_activity_isolation(tmp_path: Path) -> None:
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
    )

    assert len(report.results) == 2
    assert report.success_count == 1
    assert report.failure_count == 1
    err = next(r for r in report.results if r.error is not None)
    assert "simulated download failure" in err.error
