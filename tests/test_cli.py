"""CLI smoke tests for Phase 3.1 additions.

Focuses on argument-parsing contracts (``--incremental`` vs ``--n``
mutual exclusion, ``sync-log`` output shape). The sync pipeline itself
is covered in :mod:`tests.test_sync`; here we just make sure the CLI
surface wires through correctly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest import mock

import responses
from typer.testing import CliRunner

from onelap2strava.cli import STRAVA_TOKEN_ENDPOINT, app


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


# ---------------------------------------------------------------------------
# strava-configure: file-behavior tests (use --skip-verify to avoid the probe)
# ---------------------------------------------------------------------------


def test_strava_configure_non_interactive_writes_env(tmp_path: Path) -> None:
    """With --client-id / --client-secret given, no prompt is needed and .env is written."""
    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "12345",
            "--client-secret",
            "supersecret",
            "--env-file",
            str(env_path),
            "--skip-verify",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert env_path.exists()
    content = env_path.read_text(encoding="utf-8")
    assert "STRAVA_CLIENT_ID=12345" in content
    assert "STRAVA_CLIENT_SECRET=supersecret" in content
    assert "STRAVA_REDIRECT_URI=http://localhost:8000/callback" in content
    assert "Created" in result.stdout
    assert str(env_path) in result.stdout
    # --skip-verify should suppress the probe message entirely.
    assert "Verifying" not in result.stdout


def test_strava_configure_interactive_prompts(tmp_path: Path) -> None:
    """Without flags, the command prompts for both values (secret hidden)."""
    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        ["strava-configure", "--env-file", str(env_path), "--skip-verify"],
        input="67890\nanothersecret\n",
    )
    assert result.exit_code == 0, result.stdout
    content = env_path.read_text(encoding="utf-8")
    assert "STRAVA_CLIENT_ID=67890" in content
    assert "STRAVA_CLIENT_SECRET=anothersecret" in content


def test_strava_configure_rejects_non_integer_client_id(tmp_path: Path) -> None:
    """Client ID must parse as int; otherwise exit 1 without writing.

    This check fires before the Strava probe, so no --skip-verify needed.
    """
    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "not-a-number",
            "--client-secret",
            "x",
            "--env-file",
            str(env_path),
        ],
    )
    assert result.exit_code == 1
    assert "must be an integer" in (result.stdout + (result.stderr or ""))
    assert not env_path.exists()


def test_strava_configure_merges_existing_env(tmp_path: Path) -> None:
    """Re-running against an existing .env updates the STRAVA_* keys in place
    while preserving ordering, comments, and any unrelated keys the user added.
    """
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# my custom header\n"
        "STRAVA_CLIENT_ID=old\n"
        "STRAVA_CLIENT_SECRET=oldsecret\n"
        "STRAVA_REDIRECT_URI=http://localhost:8000/callback\n"
        "CUSTOM_KEY=keep-me\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "99",
            "--client-secret",
            "newsecret",
            "--env-file",
            str(env_path),
            "--skip-verify",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Updated" in result.stdout

    content = env_path.read_text(encoding="utf-8")
    assert "# my custom header" in content
    assert "STRAVA_CLIENT_ID=99" in content
    assert "STRAVA_CLIENT_SECRET=newsecret" in content
    assert "CUSTOM_KEY=keep-me" in content
    assert "old" not in content
    assert "oldsecret" not in content
    # Ordering: CUSTOM_KEY stays after the STRAVA_* block as it was written.
    assert content.index("STRAVA_CLIENT_ID=99") < content.index("CUSTOM_KEY=keep-me")


def test_strava_configure_appends_missing_keys(tmp_path: Path) -> None:
    """If .env has only some STRAVA_* keys, the missing ones are appended."""
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=1\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "42",
            "--client-secret",
            "sec",
            "--env-file",
            str(env_path),
            "--skip-verify",
        ],
    )
    assert result.exit_code == 0, result.stdout
    content = env_path.read_text(encoding="utf-8")
    assert "OTHER=1" in content
    assert "STRAVA_CLIENT_ID=42" in content
    assert "STRAVA_CLIENT_SECRET=sec" in content
    assert "STRAVA_REDIRECT_URI=http://localhost:8000/callback" in content


def test_strava_configure_reports_created_vs_updated(tmp_path: Path) -> None:
    """Output should say 'Created' on first run, 'Updated' on re-run."""
    env_path = tmp_path / ".env"
    first = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "1",
            "--client-secret",
            "a",
            "--env-file",
            str(env_path),
            "--skip-verify",
        ],
    )
    assert first.exit_code == 0
    assert "Created" in first.stdout

    second = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "2",
            "--client-secret",
            "b",
            "--env-file",
            str(env_path),
            "--skip-verify",
        ],
    )
    assert second.exit_code == 0
    assert "Updated" in second.stdout


# ---------------------------------------------------------------------------
# strava-configure: live Strava probe tests (use responses to mock HTTP)
# ---------------------------------------------------------------------------


@responses.activate
def test_strava_configure_probe_accepts_valid_credentials(tmp_path: Path) -> None:
    """When Strava rejects only the (fake) probe *code*, credentials are good
    and the .env gets written.
    """
    responses.add(
        responses.POST,
        STRAVA_TOKEN_ENDPOINT,
        json={
            "message": "Bad Request",
            "errors": [
                {"resource": "AuthorizationCode", "field": "code", "code": "invalid"}
            ],
        },
        status=400,
    )

    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "42",
            "--client-secret",
            "goodsecret",
            "--env-file",
            str(env_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Verifying credentials with Strava" in result.stdout
    assert "credentials accepted by Strava" in result.stdout
    assert env_path.exists()
    assert "STRAVA_CLIENT_ID=42" in env_path.read_text(encoding="utf-8")


@responses.activate
def test_strava_configure_probe_rejects_bad_client_id(tmp_path: Path) -> None:
    """A client_id-field error aborts without writing anything."""
    responses.add(
        responses.POST,
        STRAVA_TOKEN_ENDPOINT,
        json={
            "message": "Bad Request",
            "errors": [
                {"resource": "Application", "field": "client_id", "code": "invalid"}
            ],
        },
        status=400,
    )

    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "999999999",
            "--client-secret",
            "anything",
            "--env-file",
            str(env_path),
        ],
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "rejected the Client ID" in combined
    assert "No changes written" in combined
    assert not env_path.exists()


@responses.activate
def test_strava_configure_probe_rejects_bad_client_secret(tmp_path: Path) -> None:
    """A client_secret-field error aborts without touching an existing .env."""
    responses.add(
        responses.POST,
        STRAVA_TOKEN_ENDPOINT,
        json={
            "message": "Bad Request",
            "errors": [
                {"resource": "Application", "field": "client_secret", "code": "invalid"}
            ],
        },
        status=400,
    )

    env_path = tmp_path / ".env"
    previous = (
        "STRAVA_CLIENT_ID=11\n"
        "STRAVA_CLIENT_SECRET=still-good\n"
        "STRAVA_REDIRECT_URI=http://localhost:8000/callback\n"
    )
    env_path.write_text(previous, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "11",
            "--client-secret",
            "wrong-new-secret",
            "--env-file",
            str(env_path),
        ],
    )
    assert result.exit_code == 1
    assert "rejected the Client Secret" in (result.stdout + (result.stderr or ""))
    # Critical: the previously-valid .env must not have been clobbered.
    assert env_path.read_text(encoding="utf-8") == previous


@responses.activate
def test_strava_configure_probe_network_failure_surfaces_skip_hint(
    tmp_path: Path,
) -> None:
    """If the probe can't even reach Strava, we fail loudly but point at
    --skip-verify so the user isn't stuck.
    """
    import requests

    responses.add(
        responses.POST,
        STRAVA_TOKEN_ENDPOINT,
        body=requests.ConnectionError("simulated network down"),
    )

    env_path = tmp_path / ".env"
    result = runner.invoke(
        app,
        [
            "strava-configure",
            "--client-id",
            "1",
            "--client-secret",
            "s",
            "--env-file",
            str(env_path),
        ],
    )
    assert result.exit_code == 1
    combined = result.stdout + (result.stderr or "")
    assert "could not reach Strava" in combined
    assert "--skip-verify" in combined
    assert not env_path.exists()


def test_auto_sync_rejects_unknown_action() -> None:
    result = runner.invoke(app, ["auto-sync", "maybe"])
    assert result.exit_code == 2
    assert "install 或 uninstall" in (result.stdout + (result.stderr or ""))


def test_auto_sync_install_validates_every() -> None:
    result = runner.invoke(
        app, ["auto-sync", "install", "--mode", "hourly", "--every", "99"]
    )
    assert result.exit_code == 2
    assert "1–23" in (result.stdout + (result.stderr or ""))


def test_auto_sync_install_validates_at() -> None:
    result = runner.invoke(
        app, ["auto-sync", "install", "--mode", "daily", "--at", "25:00"]
    )
    assert result.exit_code == 2


def test_auto_sync_install_hourly_delegates_unix(tmp_path: Path, monkeypatch) -> None:
    bf = tmp_path / "batchfiles"
    bf.mkdir()
    (bf / "install-scheduled-sync-unix.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    monkeypatch.setattr("onelap2strava.cli._batchfiles_dir", lambda: bf)
    captured: dict = {}

    def fake_run(cmd, cwd=None, **_kwargs):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        return subprocess.CompletedProcess(cmd, 0, "")

    monkeypatch.setattr("onelap2strava.cli.subprocess.run", fake_run)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "onelap2strava.cli.shutil.which", lambda name: "/bin/bash" if name == "bash" else None
    )

    result = runner.invoke(
        app, ["auto-sync", "install", "--mode", "hourly", "--every", "3"]
    )
    assert result.exit_code == 0
    assert captured["cmd"][-2:] == ["hourly", "3"]
    assert captured["cwd"] == str(tmp_path)


def test_auto_sync_install_daily_delegates_windows(tmp_path: Path, monkeypatch) -> None:
    bf = tmp_path / "batchfiles"
    bf.mkdir()
    (bf / "install-scheduled-sync-windows.cmd").write_text("@exit 0\r\n", encoding="utf-8")

    monkeypatch.setattr("onelap2strava.cli._batchfiles_dir", lambda: bf)
    captured: dict = {}

    def fake_run(cmd, cwd=None, **_kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "")

    monkeypatch.setattr("onelap2strava.cli.subprocess.run", fake_run)
    monkeypatch.setattr(sys, "platform", "win32")

    result = runner.invoke(
        app, ["auto-sync", "install", "--mode", "daily", "--at", "07:30"]
    )
    assert result.exit_code == 0
    assert captured["cmd"][-2:] == ["daily", "07:30"]


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
