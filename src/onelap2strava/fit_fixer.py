"""Read a GCJ-02-biased Fit file, rewrite GPS coordinates as WGS-84.

All non-GPS fields (timestamps, heart rate, power, cadence, altitude,
distance, developer fields, ...) are left untouched. Only fields with names
ending in ``_lat`` or ``_long`` on record/lap/session messages are transformed.

The fit-tool library happens to expose lat/lng as ``float`` degrees (already
converting semicircles on the fly), so we can skip the unit math.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fit_tool.definition_message import DefinitionMessage
from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.record_message import RecordMessage

from .coords import gcj02_to_wgs84

# Fields we consider geographic coordinates. Using the ``_lat`` / ``_long``
# suffix match is robust across RecordMessage (``position_lat``), LapMessage
# (``start_position_lat`` / ``end_position_lat``) and SessionMessage
# (``start_position_lat``, ``nec_lat``, ``swc_lat``, ...). Non-geographic
# fields in fit use different suffixes (e.g. ``avg_left_power_phase``) so
# this filter is safe.
_LAT_SUFFIXES = ("_lat",)
_LONG_SUFFIXES = ("_long", "_lng")


def _is_lat_field(name: str) -> bool:
    return any(name.endswith(s) for s in _LAT_SUFFIXES)


def _is_long_field(name: str) -> bool:
    return any(name.endswith(s) for s in _LONG_SUFFIXES)


@dataclass
class FixResult:
    """Summary of what happened during ``fix_fit``."""

    input_path: Path
    output_path: Path
    record_points_total: int
    record_points_converted: int
    other_points_converted: int  # lap + session corners, etc.
    start_time_utc: datetime | None
    sport: str | None


def _iter_coord_pairs(message) -> list[tuple[str, str]]:
    """Return (lat_field_name, long_field_name) pairs on a message.

    Fields come unordered from the library, so we pair them by stripping
    ``_lat`` / ``_long`` / ``_lng`` suffix and matching the prefix.
    """
    lat_names = {}
    long_names = {}
    for f in message.fields:
        nm = f.name
        if _is_lat_field(nm):
            lat_names[nm.rsplit("_", 1)[0]] = nm  # e.g. "position" -> "position_lat"
        elif _is_long_field(nm):
            long_names[nm.rsplit("_", 1)[0]] = nm
    pairs: list[tuple[str, str]] = []
    for prefix, lat_name in lat_names.items():
        if prefix in long_names:
            pairs.append((lat_name, long_names[prefix]))
    return pairs


def _convert_message(message) -> int:
    """Rewrite coords on one message in-place. Returns number of points converted."""
    converted = 0
    for lat_name, long_name in _iter_coord_pairs(message):
        lat = getattr(message, lat_name, None)
        lng = getattr(message, long_name, None)
        if lat is None or lng is None:
            continue
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            continue
        new_lat, new_lng = gcj02_to_wgs84(lat_f, lng_f)
        setattr(message, lat_name, new_lat)
        setattr(message, long_name, new_lng)
        converted += 1
    return converted


@dataclass
class FitMetadata:
    """Structural facts about a fit file relevant to sync logging / dedup.

    Intentionally decoupled from :class:`FixResult` so that Phase 3.1's
    sync log can be fed from either a freshly downloaded GCJ-02 fit (at
    backfill time) or a post-fix WGS-84 fit (not currently used, but
    reserved) without coupling to the full fix pipeline.

    Coordinate fields carry whatever frame the input file used; the sync
    log treats them as an opaque pair, relying on the 500m fuzzy
    threshold to absorb the ~200-300m GCJ bias if the log ends up mixing
    frames.
    """

    path: Path
    fit_sha1: str
    start_time_utc: datetime | None
    duration_s: int | None
    start_lat: float | None
    start_lng: float | None
    sport: str | None


def _sha1_of_file(path: Path) -> str:
    """Stream the file through sha1 so we don't load multi-MB into memory."""
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_metadata(fit: FitFile) -> tuple[
    datetime | None, str | None, int | None, float | None, float | None
]:
    """Pull out start time (UTC), sport, duration, and start coordinates.

    Returns ``(start_time_utc, sport, duration_s, start_lat, start_lng)``.
    Any individual field can be ``None`` if the fit lacks the source
    message — typical for short rides that skip the session summary.
    """
    start_time_utc: datetime | None = None
    sport: str | None = None
    duration_s: int | None = None
    start_lat: float | None = None
    start_lng: float | None = None

    for r in fit.records:
        m = r.message
        if m is None or isinstance(m, DefinitionMessage):
            continue
        cls = type(m).__name__
        if cls == "SessionMessage":
            ts_ms = getattr(m, "start_time", None)
            if ts_ms is not None and start_time_utc is None:
                start_time_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            sport_val = getattr(m, "sport", None)
            if sport_val is not None and sport is None:
                sport = str(sport_val)
            # total_elapsed_time is wall-clock; total_timer_time excludes
            # auto-pauses. Wall-clock matches what users perceive as
            # "ride duration" and is what fuzzy dedup intends to compare.
            elapsed = getattr(m, "total_elapsed_time", None)
            if elapsed is None:
                elapsed = getattr(m, "total_timer_time", None)
            if elapsed is not None and duration_s is None:
                try:
                    duration_s = int(float(elapsed))
                except (TypeError, ValueError):
                    pass
            if start_lat is None:
                sl = getattr(m, "start_position_lat", None)
                sg = getattr(m, "start_position_long", None)
                if sl is not None and sg is not None:
                    try:
                        start_lat = float(sl)
                        start_lng = float(sg)
                    except (TypeError, ValueError):
                        pass
        elif cls == "ActivityMessage" and start_time_utc is None:
            ts_ms = getattr(m, "local_timestamp", None) or getattr(m, "timestamp", None)
            if ts_ms is not None:
                start_time_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

    # Fall back to the first record's timestamp + position if we still
    # have nothing. Short rides without a session summary hit this path.
    if start_time_utc is None or start_lat is None:
        for r in fit.records:
            m = r.message
            if isinstance(m, RecordMessage):
                if start_time_utc is None:
                    ts_ms = getattr(m, "timestamp", None)
                    if ts_ms is not None:
                        start_time_utc = datetime.fromtimestamp(
                            ts_ms / 1000, tz=timezone.utc
                        )
                if start_lat is None:
                    rl = getattr(m, "position_lat", None)
                    rg = getattr(m, "position_long", None)
                    if rl is not None and rg is not None:
                        try:
                            start_lat = float(rl)
                            start_lng = float(rg)
                        except (TypeError, ValueError):
                            pass
                if start_time_utc is not None and start_lat is not None:
                    break

    return start_time_utc, sport, duration_s, start_lat, start_lng


def read_fit_metadata(path: Path | str) -> FitMetadata:
    """Parse ``path`` once and return its structural metadata.

    Used by:

    - :func:`onelap2strava.sync_log.SyncLog.backfill_from_cache` to seed
      the sync log from existing ``data/cache/*.fit`` files when Phase
      3.1 is first enabled.
    - :mod:`onelap2strava.sync` to fuzzy-check a freshly downloaded fit
      against the sync log *before* running the full fix pipeline.

    Both callers pass the raw downloaded fit (GCJ-02 frame), keeping the
    stored coordinates in a single frame throughout.
    """
    path = Path(path)
    fit = FitFile.from_file(str(path))
    start_time, sport, duration_s, start_lat, start_lng = _extract_metadata(fit)
    return FitMetadata(
        path=path,
        fit_sha1=_sha1_of_file(path),
        start_time_utc=start_time,
        duration_s=duration_s,
        start_lat=start_lat,
        start_lng=start_lng,
        sport=sport,
    )


def fix_fit(input_path: Path | str, output_path: Path | str | None = None) -> FixResult:
    """Convert a GCJ-02 biased Fit to a WGS-84 Fit.

    Parameters
    ----------
    input_path:
        Path to the source ``.fit`` (typically exported from Onelap).
    output_path:
        Where to write the fixed file. Defaults to
        ``data/output/<stem>.fixed.fit`` relative to the current working
        directory.

    Returns
    -------
    FixResult with counts and metadata useful for logging and Strava dedup.
    """
    input_path = Path(input_path)
    if output_path is None:
        out_dir = Path("data/output")
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"{input_path.stem}.fixed.fit"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fit = FitFile.from_file(str(input_path))

    record_total = 0
    record_converted = 0
    other_converted = 0

    for record in fit.records:
        msg = record.message
        if msg is None or isinstance(msg, DefinitionMessage):
            continue
        if isinstance(msg, RecordMessage):
            record_total += 1
            record_converted += _convert_message(msg)
        else:
            other_converted += _convert_message(msg)

    start_time_utc, sport, _duration, _slat, _slng = _extract_metadata(fit)

    # Invalidate the stored CRC so ``to_file`` recomputes it; otherwise the
    # library raises because our GPS edits change the byte-stream checksum.
    fit.crc = None
    fit.to_file(str(output_path))

    return FixResult(
        input_path=input_path,
        output_path=output_path,
        record_points_total=record_total,
        record_points_converted=record_converted,
        other_points_converted=other_converted,
        start_time_utc=start_time_utc,
        sport=sport,
    )
