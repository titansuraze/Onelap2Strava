"""Typer CLI exposing every end-user command.

Strava-side commands:

- ``strava-configure`` : interactively write ``.env`` with Strava App credentials.
- ``auth``             : first-time authorize / refresh Strava token.
- ``token-info``       : inspect on-disk Strava token status (debug aid).

Local-file commands:

- ``fix``         : coordinate-correct a single fit file; write to data/output/.
- ``upload``      : fix + dedup + upload a single fit file.
- ``upload-dir``  : batch variant of ``upload`` for a directory of fit files.

Onelap-pull commands:

- ``onelap-login``: save Onelap session cookies (manual paste from DevTools).
- ``onelap-list`` : show recent activities on Onelap (debug aid).
- ``sync``        : pull latest Onelap activities and upload to Strava end-to-end.
- ``auto-sync``   : register/remove OS scheduled sync via ``batchfiles/`` scripts.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from .fit_fixer import fix_fit
from .onelap.auth import (
    DEFAULT_COOKIE_PATH,
    NotAuthenticatedError,
    get_authenticated_onelap_client,
    save_cookies_from_string,
)
from .onelap.client import OnelapAuthRequired, OnelapError
from .strava_auth import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_TOKEN_PATH,
    StravaCredentials,
    authorize,
    get_authenticated_client,
)
from .strava_client import upload_fit
from .sync import DEFAULT_CACHE_DIR, run_sync
from .sync_log import DEFAULT_DB_PATH, SyncLog

app = typer.Typer(
    help="Onelap -> Strava: fix GCJ-02 biased fit files and upload to Strava.",
    no_args_is_help=True,
)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.callback()
def _main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    _setup_logging(verbose)


DEFAULT_ENV_PATH = Path(".env")

_AT_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


def _batchfiles_dir() -> Path:
    """``batchfiles/`` at repository root (next to ``src/``)."""
    root = Path(__file__).resolve().parent.parent.parent
    d = root / "batchfiles"
    if not d.is_dir():
        typer.echo(
            f"[error] 未找到 {d}。auto-sync 仅在完整克隆仓库且含 batchfiles/ 时可用。",
            err=True,
        )
        raise typer.Exit(code=1)
    return d


def _validate_auto_sync_install(mode: str, every: int, at: str) -> None:
    ml = mode.lower()
    if ml not in ("hourly", "daily"):
        typer.echo("[error] --mode 须为 hourly 或 daily。", err=True)
        raise typer.Exit(code=2)
    if ml == "hourly":
        if every < 1 or every > 23:
            typer.echo("[error] --every 须在 1–23（小时）。", err=True)
            raise typer.Exit(code=2)
    else:
        m = _AT_CLOCK.match(at.strip())
        if not m:
            typer.echo(
                "[error] --at 须为 HH:MM（24 小时制），例如 22:00 或 7:30。",
                err=True,
            )
            raise typer.Exit(code=2)
        h, mn = int(m.group(1)), int(m.group(2))
        if h > 23 or mn > 59:
            typer.echo("[error] --at 时间无效。", err=True)
            raise typer.Exit(code=2)


@app.command(name="auto-sync")
def auto_sync(
    action: str = typer.Argument(
        ...,
        help="install：注册系统定时任务；uninstall：移除。",
    ),
    mode: str = typer.Option(
        "hourly",
        "--mode",
        "-m",
        help="install 时：hourly（每 N 小时）或 daily（每天固定时刻）。",
    ),
    every: int = typer.Option(
        4,
        "--every",
        "-e",
        help="install 且 hourly：间隔小时数（1–23）。",
    ),
    at: str = typer.Option(
        "22:00",
        "--at",
        help="install 且 daily：每天运行时间（HH:MM）。",
    ),
    task_name: str | None = typer.Option(
        None,
        "--task-name",
        help="仅 uninstall：Windows 计划任务名称（默认 Onelap2StravaIncrementalSync）。",
    ),
) -> None:
    """调用 ``batchfiles/`` 下的脚本注册或移除定时增量同步（非 Python 内嵌调度）。"""
    act = action.strip().lower()
    if act not in ("install", "uninstall"):
        typer.echo(
            f"[error] ACTION 须为 install 或 uninstall，收到 {action!r}。",
            err=True,
        )
        raise typer.Exit(code=2)

    bf = _batchfiles_dir()
    repo_root = bf.parent

    if act == "uninstall":
        if sys.platform == "win32":
            cmd: list[str] = [
                str(bf / "install-scheduled-sync-windows.cmd"),
                "uninstall",
            ]
            if task_name:
                cmd.append(task_name)
        else:
            bash = shutil.which("bash")
            if not bash:
                typer.echo("[error] 未找到 bash，无法执行卸载脚本。", err=True)
                raise typer.Exit(code=1)
            cmd = [bash, str(bf / "install-scheduled-sync-unix.sh"), "--remove"]
        proc = subprocess.run(cmd, cwd=str(repo_root))
        raise typer.Exit(code=proc.returncode)

    _validate_auto_sync_install(mode, every, at)
    ml = mode.lower()
    if sys.platform == "win32":
        if ml == "hourly":
            cmd = [
                str(bf / "install-scheduled-sync-windows.cmd"),
                "hourly",
                str(every),
            ]
        else:
            cmd = [str(bf / "install-scheduled-sync-windows.cmd"), "daily", at.strip()]
        proc = subprocess.run(cmd, cwd=str(repo_root))
    else:
        bash = shutil.which("bash")
        if not bash:
            typer.echo("[error] 未找到 bash，无法执行 install-scheduled-sync-unix.sh。", err=True)
            raise typer.Exit(code=1)
        if ml == "hourly":
            cmd = [bash, str(bf / "install-scheduled-sync-unix.sh"), "hourly", str(every)]
        else:
            cmd = [bash, str(bf / "install-scheduled-sync-unix.sh"), "daily", at.strip()]
        proc = subprocess.run(cmd, cwd=str(repo_root))
    raise typer.Exit(code=proc.returncode)

# Strava's official OAuth token endpoint. Note the /api/v3/ prefix — the
# short /oauth/token URL shown in older docs only serves user-facing HTML.
STRAVA_TOKEN_ENDPOINT = "https://www.strava.com/api/v3/oauth/token"


def _verify_strava_credentials(client_id: str, client_secret: str) -> tuple[bool, str]:
    """Probe the Strava token endpoint to confirm the Client ID / Secret pair
    is registered, *without* requiring the user to complete an OAuth flow.

    Trick: submit a deliberately-bogus ``code`` and read Strava's structured
    error response. Strava tells us *which* field it disliked:

    - ``field=client_id`` / ``field=client_secret`` -> the pair is bad.
    - ``resource=AuthorizationCode`` / ``field=code`` -> the pair is good,
      only our fake code was rejected — which is exactly the happy path.

    Returns ``(ok, message)``. ``ok=False`` is also produced for network
    failures and unparseable responses; callers can surface those or let
    ``--skip-verify`` bypass entirely.
    """
    import requests

    try:
        resp = requests.post(
            STRAVA_TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": "onelap2strava-probe",
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        return False, (
            f"could not reach Strava to verify credentials ({e}). "
            "Pass --skip-verify to write anyway."
        )

    # A successful token issuance from a probe code shouldn't happen; treat
    # it as "Strava is evidently happy" rather than surprising the user.
    if resp.ok:
        return True, "credentials accepted (probe unexpectedly issued a token)."

    try:
        payload = resp.json()
    except ValueError:
        return False, (
            f"unexpected Strava response ({resp.status_code}): {resp.text[:200]}"
        )

    errors = payload.get("errors") or []
    for err in errors:
        field = str(err.get("field", "")).lower()
        if field == "client_id":
            return False, "Strava rejected the Client ID (not a registered App ID)."
        if field == "client_secret":
            return False, (
                "Strava rejected the Client Secret (doesn't match this Client ID)."
            )

    for err in errors:
        resource = str(err.get("resource", ""))
        field = str(err.get("field", "")).lower()
        if resource == "AuthorizationCode" or field == "code":
            return True, "credentials accepted by Strava."

    return False, f"unexpected Strava response: {payload}"


def _merge_env_file(env_file: Path, values: dict[str, str]) -> bool:
    """Create or update ``env_file`` with the given KEY=VALUE pairs in place.

    If the file already exists, keys in ``values`` are replaced on the
    lines where they currently live (preserving ordering, comments,
    blank lines, and any unrelated keys). Missing keys are appended.

    Returns whether the file previously existed.
    """
    existed = env_file.exists()
    if existed:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# Strava API credentials",
            "# Get them from https://www.strava.com/settings/api",
        ]

    seen: set[str] = set()
    merged: list[str] = []
    for line in lines:
        replaced = False
        for key, val in values.items():
            # Match strict KEY=... form; don't touch lines like `export KEY=`
            # or `# KEY=...` so we never clobber something we don't understand.
            if line.startswith(f"{key}="):
                merged.append(f"{key}={val}")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            merged.append(line)

    for key, val in values.items():
        if key not in seen:
            merged.append(f"{key}={val}")

    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(merged) + "\n", encoding="utf-8")
    return existed


@app.command(name="strava-configure")
def strava_configure(
    client_id: str | None = typer.Option(
        None,
        "--client-id",
        help="Strava App Client ID (numeric). If omitted, you'll be prompted.",
    ),
    client_secret: str | None = typer.Option(
        None,
        "--client-secret",
        help=(
            "Strava App Client Secret. If omitted, you'll be prompted "
            "interactively (terminal hides the input)."
        ),
    ),
    redirect_uri: str = typer.Option(
        DEFAULT_REDIRECT_URI,
        "--redirect-uri",
        help="OAuth callback URI (must match your Strava App's Authorization Callback Domain).",
    ),
    env_file: Path = typer.Option(
        DEFAULT_ENV_PATH,
        "--env-file",
        help="Where to write the .env file.",
    ),
    skip_verify: bool = typer.Option(
        False,
        "--skip-verify",
        help=(
            "Skip the live Strava probe that confirms the Client ID / Secret "
            "pair is registered. Useful for offline setup or CI."
        ),
    ),
) -> None:
    """Write Strava OAuth credentials to a local ``.env`` file.

    Mirrors ``onelap-login``: one interactive command replaces the
    "create a file and fill in two lines" setup step. If ``.env``
    already exists, only the three ``STRAVA_*`` keys are updated
    in place — other keys, comments, and blank lines are preserved.

    Before writing, the command makes a live probe against Strava's
    token endpoint with a bogus ``code`` to confirm the Client ID /
    Secret pair is actually registered; if Strava rejects the pair,
    the command aborts **without touching** ``.env`` so an existing
    good config is never clobbered by a typo. Pass ``--skip-verify``
    to bypass the probe (offline setup / CI).

    **Where to get Client ID / Secret:**

    Go to https://www.strava.com/settings/api and create an App
    (Category: Data Importer, Website: any reachable URL,
    **Authorization Callback Domain: localhost**). The page then
    shows your Client ID and Client Secret.

    After this command succeeds, run ``onelap2strava auth`` to
    complete OAuth.
    """
    if client_id is None:
        client_id = typer.prompt("Strava Client ID")
    client_id = client_id.strip()
    try:
        int(client_id)
    except ValueError:
        typer.echo(
            f"[error] Client ID must be an integer, got {client_id!r}.",
            err=True,
        )
        raise typer.Exit(code=1)

    if client_secret is None:
        client_secret = typer.prompt("Strava Client Secret", hide_input=True)
    client_secret = client_secret.strip()
    if not client_secret:
        typer.echo("[error] Client Secret must not be empty.", err=True)
        raise typer.Exit(code=1)

    # Verify BEFORE writing: if the probe fails we don't want to have
    # already overwritten a previously-working set of STRAVA_* lines in
    # the user's .env with bad values.
    if not skip_verify:
        typer.echo("Verifying credentials with Strava...")
        ok, msg = _verify_strava_credentials(client_id, client_secret)
        if ok:
            typer.echo(f"[ok]    {msg}")
        else:
            typer.echo(f"[error] {msg}", err=True)
            typer.echo(
                "No changes written. Double-check Client ID / Secret at "
                "https://www.strava.com/settings/api and re-run. "
                "Pass --skip-verify to write anyway.",
                err=True,
            )
            raise typer.Exit(code=1)

    existed = _merge_env_file(
        env_file,
        {
            "STRAVA_CLIENT_ID": client_id,
            "STRAVA_CLIENT_SECRET": client_secret,
            "STRAVA_REDIRECT_URI": redirect_uri,
        },
    )
    verb = "Updated" if existed else "Created"
    typer.echo(
        f"{verb} {env_file} (STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET / STRAVA_REDIRECT_URI)."
    )
    typer.echo("Next step: `uv run onelap2strava auth` to authorize Strava.")


@app.command()
def auth() -> None:
    """Run first-time Strava OAuth (opens browser, waits for callback)."""
    creds = StravaCredentials.from_env()
    authorize(creds)


@app.command()
def fix(
    fit_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Where to write the fixed fit (default: data/output/)."
    ),
) -> None:
    """Correct GCJ-02 coordinates in FIT_FILE and write the fixed version."""
    result = fix_fit(fit_file, output)
    typer.echo(
        f"Fixed: {result.output_path} "
        f"(record points converted: {result.record_points_converted}/{result.record_points_total}, "
        f"other: {result.other_points_converted}, "
        f"start: {result.start_time_utc})"
    )


def _upload_one(
    fit_file: Path,
    *,
    name: str | None,
    force: bool,
) -> int:
    """Shared path for single-file and directory uploads. Returns process exit code contribution."""
    typer.echo(f"--> {fit_file.name}")
    result = fix_fit(fit_file)
    if result.start_time_utc is None:
        typer.echo(
            f"    [error] could not determine start time from {fit_file}; skipping", err=True
        )
        return 1
    typer.echo(
        f"    fixed {result.record_points_converted} points; "
        f"start={result.start_time_utc.isoformat()}"
    )

    client = get_authenticated_client()
    outcome = upload_fit(
        client,
        result.output_path,
        start_time_utc=result.start_time_utc,
        name=name,
        force=force,
    )
    typer.echo(f"    {outcome.pretty()}")
    return 0


@app.command()
def upload(
    fit_file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    name: str | None = typer.Option(None, "--name", help="Override the activity name."),
    force: bool = typer.Option(
        False, "--force", help="Skip the local time-window duplicate check."
    ),
) -> None:
    """Fix FIT_FILE then upload to Strava (with duplicate detection)."""
    sys.exit(_upload_one(fit_file, name=name, force=force))


@app.command(name="upload-dir")
def upload_dir(
    directory: Path = typer.Argument(..., exists=True, file_okay=False, readable=True),
    pattern: str = typer.Option("*.fit", "--pattern", help="Glob to select files."),
    force: bool = typer.Option(
        False, "--force", help="Skip the local time-window duplicate check."
    ),
) -> None:
    """Batch variant of ``upload``: process every fit file in DIRECTORY."""
    files = sorted(directory.glob(pattern))
    if not files:
        typer.echo(f"No files matching {pattern} in {directory}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Processing {len(files)} file(s) from {directory}...")
    failures = 0
    for fit_file in files:
        try:
            failures += _upload_one(fit_file, name=None, force=force)
        except Exception as e:  # noqa: BLE001 - want to continue on one file's failure
            typer.echo(f"    [error] {fit_file.name}: {e}", err=True)
            failures += 1
    if failures:
        raise typer.Exit(code=1)


@app.command(name="onelap-login")
def onelap_login(
    cookie_string: str | None = typer.Option(
        None,
        "--cookie",
        help=(
            "Raw 'Cookie' header from DevTools (e.g. 'OTOKEN=...; XSRF-TOKEN=...'). "
            "If omitted, you'll be prompted interactively (terminal hides the input)."
        ),
    ),
) -> None:
    """Save Onelap session cookies for later use by ``sync``.

    **How to get the Cookie value:**

    1. Open http://u.onelap.cn/analysis/list in a logged-in Chrome / Edge
       (the page should show a blob of JSON; if it redirects to a login
       form, log in there first).
    2. Press F12 to open DevTools -> Network tab -> press F5 to refresh.
    3. In the Network list, click the request named ``list`` (the very
       first entry, document type — it's the page itself).
    4. In the right pane, open **Headers** -> **Request Headers** -> find
       the line starting with ``Cookie:`` -> copy everything after the
       colon, on a single line.
    5. Paste at the prompt (input is hidden for privacy) or pass it via
       ``--cookie "..."``.

    The saved cookie is stored at ``data/.onelap_cookies.json`` and is
    reused by ``sync`` / ``onelap-list`` until Onelap invalidates the
    session (observed lifetime: several days to a week+). When it
    expires the CLI surfaces a ``cookies likely expired`` message and
    you re-run this command.

    See contexts/phase2-onelap-api.md for endpoint details and
    contexts/phase2-onelap-scraping.md for why browser-autoread was
    explored then abandoned (ABE on Chrome/Edge 125+ can't be reliably
    decrypted even with admin on Windows).
    """
    if cookie_string is None:
        cookie_string = typer.prompt(
            "Paste your Onelap 'Cookie' header value",
            hide_input=True,
        )
    try:
        jar = save_cookies_from_string(cookie_string, DEFAULT_COOKIE_PATH)
    except ValueError as e:
        typer.echo(f"[error] {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Saved {len(jar.cookies)} cookie entries to {DEFAULT_COOKIE_PATH}."
    )

    # Live probe: confirm the cookies work against the real API. This is
    # the authoritative "is the user logged in" check — without it the
    # user would only discover a bad paste when `sync` fails later.
    try:
        client = get_authenticated_onelap_client()
        activities = client.list_activities(limit=1)
    except OnelapAuthRequired as e:
        typer.echo(
            f"[error] Cookies saved but rejected by Onelap: {e}\n"
            "Log in again in your browser, then re-run this command.",
            err=True,
        )
        raise typer.Exit(code=1)
    except (OnelapError, NotAuthenticatedError) as e:
        typer.echo(
            f"[warn]  cookie saved but verification request failed: {e}",
            err=True,
        )
        raise typer.Exit(code=1)
    if activities:
        typer.echo(
            f"[ok]    verified: latest activity = {activities[0].short_description()}"
        )
    else:
        typer.echo("[ok]    verified: no activities on account yet.")


@app.command(name="onelap-list")
def onelap_list(
    n: int = typer.Option(5, "--n", "-n", help="How many recent activities to show."),
) -> None:
    """List the latest Onelap activities (debug aid; no upload)."""
    try:
        client = get_authenticated_onelap_client()
        activities = client.list_activities(limit=n)
    except NotAuthenticatedError as e:
        typer.echo(f"[error] {e}", err=True)
        raise typer.Exit(code=1)
    except (OnelapAuthRequired, OnelapError) as e:
        typer.echo(f"[error] {e}", err=True)
        raise typer.Exit(code=1)

    if not activities:
        typer.echo("No activities found.")
        return
    for i, a in enumerate(activities, 1):
        typer.echo(f"  {i:>2}. {a.short_description()}")


@app.command()
def sync(
    n: int = typer.Option(1, "--n", "-n", help="Number of most recent activities to sync."),
    incremental: bool = typer.Option(
        False,
        "--incremental",
        help=(
            "Pull all activities newer than the last successful sync "
            "(based on the local sync log). Mutually exclusive with --n."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the local fuzzy dedup AND Strava's ±10min duplicate check.",
    ),
    name: str | None = typer.Option(
        None, "--name", help="Override the Strava activity name (applies to every synced fit)."
    ),
) -> None:
    """Pull the latest Onelap activities and upload them to Strava end-to-end.

    Local sync log at ``data/.sync.db`` records every handled activity
    so repeated runs do not re-upload rides that were already synced
    (even if they got re-exported with slightly different bytes).
    """
    # `--n` has a non-sentinel default (1), so we use `incremental`
    # explicitly rather than "n was passed" to disambiguate intent.
    if incremental and n != 1:
        typer.echo(
            "[error] --incremental and --n are mutually exclusive; "
            "--incremental always pulls everything new.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        report = run_sync(
            limit=n,
            force=force,
            name=name,
            incremental=incremental,
            cache_dir=DEFAULT_CACHE_DIR,
        )
    except NotAuthenticatedError as e:
        typer.echo(f"[error] {e}", err=True)
        raise typer.Exit(code=1)
    except (OnelapAuthRequired, OnelapError) as e:
        typer.echo(f"[error] onelap: {e}", err=True)
        raise typer.Exit(code=1)

    if not report.results:
        if incremental:
            typer.echo("No new activities since last sync.")
        else:
            typer.echo("No activities on Onelap to sync.")
        return

    for r in report.results:
        typer.echo(r.pretty())
    typer.echo(
        f"summary: ok={report.success_count} "
        f"duplicate={report.skipped_duplicate_count} "
        f"fail={report.failure_count}"
    )
    if report.failure_count:
        raise typer.Exit(code=1)


@app.command(name="sync-log")
def sync_log_cmd(
    n: int = typer.Option(20, "--n", "-n", help="How many recent rows to display."),
) -> None:
    """Show the most recent rows from the local sync log (debug / audit).

    Useful to answer "did my last sync actually touch Strava?" or
    "which activities were skipped as duplicates?". The log lives at
    ``data/.sync.db``; deleting that file resets the state (the next
    ``sync`` will backfill from ``data/cache/`` if present).
    """
    if not DEFAULT_DB_PATH.exists():
        typer.echo(f"No sync log yet. Run `sync` first (expected at {DEFAULT_DB_PATH}).")
        return
    with SyncLog.open(DEFAULT_DB_PATH) as log:
        rows = log.recent(limit=n)
    if not rows:
        typer.echo("Sync log is empty.")
        return
    for row in rows:
        start = row.start_time_utc.astimezone().strftime("%Y-%m-%d %H:%M")
        strava = f"strava={row.strava_activity_id}" if row.strava_activity_id else ""
        typer.echo(
            f"  {row.synced_at.astimezone().strftime('%Y-%m-%d %H:%M')} "
            f"status={row.status:<10} start={start} "
            f"onelap={row.onelap_activity_id} {strava}"
        )


@app.command(name="token-info")
def token_info() -> None:
    """Show the current on-disk token status (debug aid)."""
    if not DEFAULT_TOKEN_PATH.exists():
        typer.echo("No token file. Run `onelap2strava auth` first.")
        raise typer.Exit(code=1)
    # Touch the client to force a refresh if needed.
    get_authenticated_client(interactive=False)
    import json
    data = json.loads(DEFAULT_TOKEN_PATH.read_text(encoding="utf-8"))
    from datetime import datetime, timezone
    exp = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc)
    typer.echo(f"Token file: {DEFAULT_TOKEN_PATH}")
    typer.echo(f"Expires at: {exp.isoformat()}")


if __name__ == "__main__":
    app()
