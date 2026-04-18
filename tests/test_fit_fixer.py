"""Real-data regression: verify GCJ-02 bias is removed on actual fit files.

The two fixtures in ``test_data/`` are the SAME route ridden on TWO DIFFERENT
occasions:

- ``MAGENE_C506_bias.fit``    - exported from Onelap, GCJ-02 biased.
- ``MAGENE_C506_correct.fit`` - pulled straight off the Magene head unit,
                                 native WGS-84 ground truth.

Because they are two independent rides, there is irreducible natural
difference between them (GPS noise, slightly different lanes, slightly
different path at intersections). So we do NOT test point-by-point
equality. Instead we test the signature of the GCJ-02 bias:

- BEFORE fix:  the track is systematically offset in one direction
               (non-zero mean offset vector with large magnitude).
- AFTER fix:   the remaining offset to ground truth is random (mean
               offset vector magnitude near zero, comparable to GPS noise).

These assertions verify the bias has been removed without demanding a
degree of precision the data can't supply.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path

import pytest

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.record_message import RecordMessage

from onelap2strava.coords import haversine_m
from onelap2strava.fit_fixer import fix_fit, read_fit_metadata

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "test_data"
BIAS_FIT = FIXTURE_DIR / "MAGENE_C506_bias.fit"
CORRECT_FIT = FIXTURE_DIR / "MAGENE_C506_correct.fit"

# The fit fixtures contain real GPS traces and are intentionally NOT
# committed to the repo (see test_data/README.md). Skip the whole module
# when they are absent so a fresh clone can still run `pytest` cleanly.
pytestmark = pytest.mark.skipif(
    not (BIAS_FIT.exists() and CORRECT_FIT.exists()),
    reason=(
        "test_data fit fixtures not available; "
        "see test_data/README.md for what to drop in."
    ),
)


def _load_track(path: Path) -> list[tuple[float, float]]:
    ff = FitFile.from_file(str(path))
    pts: list[tuple[float, float]] = []
    for r in ff.records:
        m = r.message
        if isinstance(m, RecordMessage) and m.position_lat is not None and m.position_long is not None:
            pts.append((m.position_lat, m.position_long))
    return pts


def _offset_to_nearest(
    track: list[tuple[float, float]],
    reference: list[tuple[float, float]],
) -> list[tuple[float, float, float]]:
    """For each point in ``track``, find nearest point in ``reference`` and
    return (east_m, north_m, distance_m) offsets.

    ``east_m`` / ``north_m`` are signed offsets in meters (local tangent plane
    approximation anchored at ``reference``'s first point). Using signed
    components lets us detect a systematic directional bias (their mean will
    be far from zero for GCJ-02, near zero for random GPS noise).

    O(N*M) is fine for ~3.7k x ~3.4k points on this fixture (fractions of a
    second); no need to build a KD-tree.
    """
    if not reference:
        return []
    ref_lat0 = reference[0][0]
    # Meters per degree at this latitude; good enough for a small bounding box.
    m_per_deg_lat = 111_320.0
    m_per_deg_lng = 111_320.0 * math.cos(math.radians(ref_lat0))

    # Precompute reference as local meters for fast squared-distance search.
    ref_xy = [
        ((lng - reference[0][1]) * m_per_deg_lng, (lat - ref_lat0) * m_per_deg_lat)
        for lat, lng in reference
    ]

    out: list[tuple[float, float, float]] = []
    for lat, lng in track:
        px = (lng - reference[0][1]) * m_per_deg_lng
        py = (lat - ref_lat0) * m_per_deg_lat
        best_idx = 0
        best_d2 = float("inf")
        for i, (rx, ry) in enumerate(ref_xy):
            dx = px - rx
            dy = py - ry
            d2 = dx * dx + dy * dy
            if d2 < best_d2:
                best_d2 = d2
                best_idx = i
        rlat, rlng = reference[best_idx]
        # Report offsets track-point minus nearest reference, in meters.
        east_m = (lng - rlng) * m_per_deg_lng
        north_m = (lat - rlat) * m_per_deg_lat
        dist_m = haversine_m(lat, lng, rlat, rlng)
        out.append((east_m, north_m, dist_m))
    return out


def _summarize(offsets: list[tuple[float, float, float]]) -> dict[str, float]:
    east = [o[0] for o in offsets]
    north = [o[1] for o in offsets]
    dist = [o[2] for o in offsets]
    mean_east = statistics.fmean(east)
    mean_north = statistics.fmean(north)
    mean_vec_mag = math.hypot(mean_east, mean_north)
    return {
        "n": len(offsets),
        "mean_east_m": mean_east,
        "mean_north_m": mean_north,
        "mean_offset_vector_mag_m": mean_vec_mag,
        "p50_distance_m": statistics.median(dist),
        "p95_distance_m": (
            sorted(dist)[int(0.95 * len(dist))] if len(dist) >= 20 else max(dist)
        ),
        "max_distance_m": max(dist),
    }


@pytest.fixture(scope="module")
def summaries(tmp_path_factory) -> dict[str, dict[str, float]]:
    """Run fix_fit once, compare both bias (pre-fix) and fixed tracks
    against the correct reference, and return summary stats."""
    out_path = tmp_path_factory.mktemp("fixer") / "bias.fixed.fit"
    fix_fit(BIAS_FIT, out_path)

    bias_track = _load_track(BIAS_FIT)
    fixed_track = _load_track(out_path)
    correct_track = _load_track(CORRECT_FIT)

    assert len(bias_track) > 100, "bias fixture has too few GPS points"
    assert len(fixed_track) == len(bias_track), "fix_fit dropped GPS points"
    assert len(correct_track) > 100, "correct fixture has too few GPS points"

    bias_offsets = _offset_to_nearest(bias_track, correct_track)
    fixed_offsets = _offset_to_nearest(fixed_track, correct_track)

    return {
        "bias": _summarize(bias_offsets),
        "fixed": _summarize(fixed_offsets),
    }


def test_systematic_offset_is_eliminated(summaries):
    """Core invariant: after fix, mean offset vector magnitude drops sharply.

    The mean offset vector is the signature distinguishing a systematic
    projection bias (GCJ-02) from random GPS noise. Before fix it should be
    hundreds of meters; after fix it should be within GPS-noise scale.
    """
    b = summaries["bias"]
    f = summaries["fixed"]

    # Absolute: bias must be large, fixed must be small. Thresholds are
    # deliberately loose (for mainland China GCJ-02 is typically 300-500m;
    # GPS jitter + two-ride lane differences are typically < 30m).
    assert b["mean_offset_vector_mag_m"] > 100, (
        f"bias track's systematic offset vector was only "
        f"{b['mean_offset_vector_mag_m']:.1f}m; expected > 100m"
    )
    assert f["mean_offset_vector_mag_m"] < 30, (
        f"fixed track still has systematic offset "
        f"{f['mean_offset_vector_mag_m']:.1f}m; expected < 30m"
    )

    # Relative: the fix must reduce the systematic offset by at least 5x,
    # regardless of absolute thresholds.
    improvement_ratio = b["mean_offset_vector_mag_m"] / max(f["mean_offset_vector_mag_m"], 1e-6)
    assert improvement_ratio > 5, (
        f"fix only reduced systematic offset by {improvement_ratio:.1f}x; "
        f"expected > 5x improvement"
    )


def test_distance_distribution_drops_to_gps_noise_scale(summaries):
    """Distribution-level sanity: P50 distance should drop dramatically after fix.

    This catches the case where the fix reduces bias in the aggregate vector
    sense but individual point distances are still huge (which would indicate
    the conversion went the wrong direction for some region).
    """
    b = summaries["bias"]
    f = summaries["fixed"]

    assert f["p50_distance_m"] < b["p50_distance_m"] / 3, (
        f"fixed P50 distance ({f['p50_distance_m']:.1f}m) should be well under "
        f"bias P50 ({b['p50_distance_m']:.1f}m)"
    )
    # Give a loose absolute ceiling to guard against regression; two rides on
    # the same route with GPS noise should be within ~80m on median.
    assert f["p50_distance_m"] < 80, (
        f"fixed P50 distance is {f['p50_distance_m']:.1f}m; expected < 80m "
        f"(GPS noise + lane differences scale)"
    )


def test_read_fit_metadata_on_real_fixture():
    """Phase 3.1: the sync log seeds itself from fits like this one.

    We only assert basic shape here — exact values would tie us to the
    private fixture that's not in version control.
    """
    meta = read_fit_metadata(BIAS_FIT)
    assert meta.fit_sha1 and len(meta.fit_sha1) == 40
    assert meta.start_time_utc is not None
    assert meta.start_time_utc.tzinfo is not None
    # A real ride has non-trivial duration and valid coordinates.
    assert meta.duration_s is None or meta.duration_s > 60
    assert meta.start_lat is not None and -90 < meta.start_lat < 90
    assert meta.start_lng is not None and -180 < meta.start_lng < 180


def test_print_summary(summaries, capsys):
    """Not really an assertion test; prints the comparison for humans.

    Run with ``uv run pytest tests/test_fit_fixer.py::test_print_summary -s``
    to see the numbers used to validate the fix in practice.
    """
    with capsys.disabled():
        print()
        for label, s in summaries.items():
            print(f"[{label}]")
            for k, v in s.items():
                print(f"  {k:>28}: {v:,.2f}" if isinstance(v, float) else f"  {k:>28}: {v}")
