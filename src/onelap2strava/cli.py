"""Typer CLI exposing the four sub-commands.

- ``auth``        : first-time authorize / refresh Strava token.
- ``fix``         : coordinate-correct a single fit file; write to data/output/.
- ``upload``      : fix + dedup + upload a single fit file.
- ``upload-dir``  : batch variant of ``upload`` for a directory of fit files.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import typer

from .fit_fixer import fix_fit
from .strava_auth import DEFAULT_TOKEN_PATH, StravaCredentials, authorize, get_authenticated_client
from .strava_client import upload_fit

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
