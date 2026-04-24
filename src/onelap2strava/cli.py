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
- ``mark-manual`` : record Onelap ids to skip in ``sync`` (e.g. you uploaded a local fit).
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
        help="install: register OS scheduler; uninstall: remove it.",
    ),
    mode: str = typer.Option(
        "hourly",
        "--mode",
        "-m",
        help="For install: hourly (every N hours) or daily (fixed clock time).",
    ),
    every: int = typer.Option(
        4,
        "--every",
        "-e",
        help="For install + hourly: interval in hours (1–23).",
    ),
    at: str = typer.Option(
        "22:00",
        "--at",
        help="For install + daily: time of day (HH:MM, 24h).",
    ),
    task_name: str | None = typer.Option(
        None,
        "--task-name",
        help="For uninstall on Windows only: scheduled task name (default: Onelap2StravaIncrementalSync).",
    ),
) -> None:
    """Register or remove incremental sync via scripts in ``batchfiles/`` (not an in-process scheduler)."""
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
    bearer: str | None = typer.Option(
        None,
        "--bearer",
        help=(
            "Optional: Authorization JWT from the same request as fit download "
            "(DevTools request headers, paste token only, without the word 'Bearer '). "
            "If omitted, any existing token in data/.onelap_cookies.json is kept when updating cookies."
        ),
    ),
) -> None:
    """Save Onelap session cookies (and optional JWT) for ``sync`` / ``onelap-list``.

    **1) Cookie（必填）** — 在已登录的 Chrome/Edge 中打开 **``https://u.onelap.cn/record``**，
    按 F12 → **Network**，刷新。在 Filter 中输入 **``u.onelap``** 或 **``otm``**，点任意一条
    **到 ``u.onelap.cn`` 的 XHR**（应返回 **JSON**；例如 ``ride_record``、``list`` 等）。
    在 **Request Headers** 中复制整行 **``Cookie:``** 后的内容；若 OTM 接口仅含少量
    站点 Cookie 但带 **Bearer** 能返回 JSON，**完整一行并非必须**（以 ``onelap-login`` 末尾 **[ok] 验证** 为准）。

    **2) Bearer（常见为必填）** — 在同一条或另一条 **成功 200** 的 ``u.onelap.cn`` 请求上，
    复制 **``Authorization:``** 里 **``Bearer `** 后面的一整段（不要写单词 ``Bearer``）。
    交互模式下在粘贴 Cookie 后会再询问；也可用 ``--bearer "eyJ..."``。

    数据写入 ``data/.onelap_cookies.json``。仅重登 Cookie 时**不传** ``--bearer`` 会**保留**
    已保存的 ``bearer``。若仍提示登录/HTML，先确认 Cookie 为**完整**一行，并加上 ``--bearer``。

    见 ``contexts/phase2-onelap-api.md``。顽鹿改版时可设环境变量 **``ONELAP_LIST_URL``**
    为抓包得到的活动列表 JSON 的**完整 URL**（覆盖内置候选端点）。

    浏览器自动读 Cookie 已放弃（见 ``phase2-onelap-scraping.md``，Chrome/Edge ABE）。
    """
    interactive_cookie = cookie_string is None
    if cookie_string is None:
        cookie_string = typer.prompt(
            "Paste your Onelap 'Cookie' header value (full line from u.onelap.cn XHR)",
            hide_input=True,
        )
    if interactive_cookie and bearer is None:
        typer.echo(
            "Optional: paste Authorization token only (after 'Bearer ' in DevTools), "
            "or press Enter to skip / keep a previously saved token:"
        )
        line = input().strip()
        if line:
            bearer = line
    try:
        jar = save_cookies_from_string(
            cookie_string, DEFAULT_COOKIE_PATH, bearer=bearer
        )
    except ValueError as e:
        typer.echo(f"[error] {e}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"Saved {len(jar.cookies)} cookie entries to {DEFAULT_COOKIE_PATH}."
    )
    if jar.bearer:
        typer.echo("Also stored Authorization bearer token (for OTM /record APIs).")

    if len(jar.cookies) < 5 or "OTOKEN" not in jar.cookies:
        typer.echo(
            "[hint]  当前 Cookie 项较少或未含 OTOKEN=；**若等下为 [ok] 可忽略本提示**。"
            "若**未**通过验证，再从 u.onelap.cn 某条 200+JSON 的 XHR 复制**完整** Cookie: 行，"
            "并保留 Bearer（顽鹿 OTM 常只需少量站点 Cookie + JWT）。",
            err=True,
        )

    # Live probe: confirm the cookies work against the real API. This is
    # the authoritative "is the user logged in" check — without it the
    # user would only discover a bad paste when `sync` fails later.
    try:
        client = get_authenticated_onelap_client()
        activities = client.list_activities(limit=1)
    except OnelapAuthRequired as e:
        typer.echo(f"[error] 顽鹿未返回 JSON 列表（多像是登录态无效）: {e}", err=True)
        typer.echo(
            "排查: (1) 在已登录的页面打开 DevTools → Network，过滤 u.onelap，点一条 200 且 "
            "Preview/Response 为 JSON 的请求；(2) 复制**该请求**的完整 Cookie: 行，不要从 "
            "Application → Cookies 里只勾选几列；(3) 同一次粘贴保留 --bearer；(4) 若仍只有 HTML，"
            "在 PowerShell 设置环境变量 ONELAP_LIST_URL=该条请求的完整“请求 URL”后再重试。",
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


@app.command("mark-manual")
def mark_manual(
    activity_ids: list[str] = typer.Argument(
        ...,
        help="顽鹿活动 id，与 sync 日志里 id= 一致，可一次填多个。",
    ),
) -> None:
    """本工具外已处理或放弃自动同步的活动：写入本地日志，之后 ``sync`` 会跳过这些 id。

    典型场景：顽鹿 FIT 在 CDN 上 404、你已用 ``upload`` 把本地 fit 传上 Strava。
    """
    with SyncLog.open(DEFAULT_DB_PATH) as log:
        for aid in activity_ids:
            log.mark_onelap_manual(aid)
    for aid in activity_ids:
        typer.echo(f"marked Onelap activity {aid} as manual (skipped by sync).")


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
