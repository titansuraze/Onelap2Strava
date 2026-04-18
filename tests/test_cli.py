"""CLI smoke tests for Phase 3.1 additions.

Focuses on argument-parsing contracts (``--incremental`` vs ``--n``
mutual exclusion, ``sync-log`` output shape). The sync pipeline itself
is covered in :mod:`tests.test_sync`; here we just make sure the CLI
surface wires through correctly.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from onelap2strava.cli import app


runner = CliRunner()


def test_sync_rejects_incremental_plus_explicit_n() -> None:
    """Both flags together is ambiguous; CLI should refuse with exit 2."""
    result = runner.invoke(app, ["sync", "--incremental", "--n", "5"])
    assert result.exit_code == 2
    assert "mutually exclusive" in (result.stdout + (result.stderr or ""))


def test_sync_log_reports_missing_db(tmp_path: Path, monkeypatch) -> None:
    """With no DB on disk, the sync-log subcommand emits a hint rather than crashing."""
    fake_path = tmp_path / "nope.db"
    monkeypatch.setattr("onelap2strava.cli.DEFAULT_DB_PATH", fake_path)

    result = runner.invoke(app, ["sync-log"])
    assert result.exit_code == 0
    assert "No sync log yet" in result.stdout


def test_sync_log_shows_rows(tmp_path: Path, monkeypatch) -> None:
    """Populating the log then calling sync-log prints one line per row."""
    from datetime import datetime, timezone

    from onelap2strava.sync_log import STATUS_OK, SyncLog

    db_path = tmp_path / ".sync.db"
    with SyncLog.open(db_path) as log:
        log.record_sync(
            onelap_activity_id="demo-1",
            fit_sha1="abc",
            start_time_utc=datetime(2025, 4, 1, 10, 0, tzinfo=timezone.utc),
            duration_s=3600,
            start_lat=39.9,
            start_lng=116.4,
            strava_activity_id=42,
            status=STATUS_OK,
        )

    monkeypatch.setattr("onelap2strava.cli.DEFAULT_DB_PATH", db_path)
    result = runner.invoke(app, ["sync-log"])
    assert result.exit_code == 0
    assert "demo-1" in result.stdout
    assert "status=ok" in result.stdout
    assert "strava=42" in result.stdout


def test_sync_with_default_n_invokes_run_sync(monkeypatch) -> None:
    """Default invocation passes limit=1, incremental=False to run_sync."""
    called: dict = {}

    def fake_run_sync(**kwargs):
        called.update(kwargs)

        class _EmptyReport:
            results: list = []
            success_count = 0
            failure_count = 0
            skipped_duplicate_count = 0

        return _EmptyReport()

    monkeypatch.setattr("onelap2strava.cli.run_sync", fake_run_sync)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert called["limit"] == 1
    assert called["incremental"] is False
    assert called["force"] is False


def test_sync_incremental_flag_passes_through(monkeypatch) -> None:
    """`--incremental` alone should flow through to run_sync."""
    called: dict = {}

    def fake_run_sync(**kwargs):
        called.update(kwargs)

        class _EmptyReport:
            results: list = []
            success_count = 0
            failure_count = 0
            skipped_duplicate_count = 0

        return _EmptyReport()

    monkeypatch.setattr("onelap2strava.cli.run_sync", fake_run_sync)
    result = runner.invoke(app, ["sync", "--incremental"])
    assert result.exit_code == 0
    assert called["incremental"] is True
    # limit is still passed but ignored by run_sync in incremental mode.
    assert called["limit"] == 1
    assert "No new activities" in result.stdout
