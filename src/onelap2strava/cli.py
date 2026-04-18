"""Typer CLI exposing every end-user command.

Strava-side commands:

- ``auth``        : first-time authorize / refresh Strava token.
- ``token-info``  : inspect on-disk Strava token status (debug aid).

Local-file commands:

- ``fix``         : coordinate-correct a single fit file; write to data/output/.
- ``upload``      : fix + dedup + upload a single fit file.
- ``upload-dir``  : batch variant of ``upload`` for a directory of fit files.

Onelap-pull commands:

- ``onelap-login``: save Onelap session cookies (manual paste from DevTools).
- ``onelap-list`` : show recent activities on Onelap (debug aid).
- ``sync``        : pull latest Onelap activities and upload to Strava end-to-end.
"""

from __future__ import annotations

import logging
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
from .strava_auth import DEFAULT_TOKEN_PATH, StravaCredentials, authorize, get_authenticated_client
from .strava_client import upload_fit
from .sync import DEFAULT_CACHE_DIR, run_sync

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
    force: bool = typer.Option(
        False, "--force", help="Skip the local time-window duplicate check on Strava."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Override the Strava activity name (applies to every synced fit)."
    ),
) -> None:
    """Pull the latest Onelap activities and upload them to Strava end-to-end."""
    try:
        report = run_sync(limit=n, force=force, name=name, cache_dir=DEFAULT_CACHE_DIR)
    except NotAuthenticatedError as e:
        typer.echo(f"[error] {e}", err=True)
        raise typer.Exit(code=1)
    except (OnelapAuthRequired, OnelapError) as e:
        typer.echo(f"[error] onelap: {e}", err=True)
        raise typer.Exit(code=1)

    if not report.results:
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
