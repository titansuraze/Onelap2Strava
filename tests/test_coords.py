"""Mathematical round-trip tests for the coordinate conversion.

These tests validate the pure function; they do not touch any fit files.
"""

from __future__ import annotations

import pytest

from onelap2strava.coords import (
    gcj02_to_wgs84,
    haversine_m,
    in_china,
    wgs84_to_gcj02,
)

# A handful of representative WGS-84 points spread across China.
CHINA_POINTS = [
    (39.9087, 116.3975),  # Beijing (Tiananmen)
    (31.2304, 121.4737),  # Shanghai
    (22.5431, 114.0579),  # Shenzhen
    (23.1291, 113.2644),  # Guangzhou
    (30.5728, 104.0668),  # Chengdu
    (31.1710, 121.4159),  # A point from the actual test fixture
]

OVERSEAS_POINTS = [
    (37.7749, -122.4194),  # San Francisco
    (51.5074, -0.1278),  # London
    (-33.8688, 151.2093),  # Sydney
    (35.6762, 139.6503),  # Tokyo
]


@pytest.mark.parametrize("lat,lng", CHINA_POINTS)
def test_wgs_to_gcj_back_to_wgs_roundtrip(lat: float, lng: float) -> None:
    """A WGS point -> GCJ -> WGS should return the starting point very closely."""
    gcj_lat, gcj_lng = wgs84_to_gcj02(lat, lng)
    back_lat, back_lng = gcj02_to_wgs84(gcj_lat, gcj_lng)
    assert abs(back_lat - lat) < 1e-6, f"lat round-trip error {back_lat - lat}"
    assert abs(back_lng - lng) < 1e-6, f"lng round-trip error {back_lng - lng}"


@pytest.mark.parametrize("lat,lng", CHINA_POINTS)
def test_gcj_offset_is_in_expected_range(lat: float, lng: float) -> None:
    """Confirm GCJ-02 actually offsets points by ~100-800 meters inside China."""
    gcj_lat, gcj_lng = wgs84_to_gcj02(lat, lng)
    offset_m = haversine_m(lat, lng, gcj_lat, gcj_lng)
    assert 100 < offset_m < 800, (
        f"GCJ offset at ({lat},{lng}) was {offset_m:.1f}m, outside expected range"
    )


@pytest.mark.parametrize("lat,lng", OVERSEAS_POINTS)
def test_overseas_points_are_passthrough(lat: float, lng: float) -> None:
    """Outside mainland China we MUST NOT transform; otherwise we introduce bias."""
    gcj_lat, gcj_lng = wgs84_to_gcj02(lat, lng)
    back_lat, back_lng = gcj02_to_wgs84(lat, lng)
    assert (gcj_lat, gcj_lng) == (lat, lng)
    assert (back_lat, back_lng) == (lat, lng)


def test_in_china_bounds() -> None:
    assert in_china(39.9, 116.4)
    assert not in_china(37.77, -122.41)
    assert not in_china(51.5, -0.12)


def test_haversine_known_distance() -> None:
    # Approximate distance Beijing Tiananmen -> Shanghai Bund is ~1065 km.
    d = haversine_m(39.9087, 116.3975, 31.2304, 121.4737)
    assert 1_050_000 < d < 1_080_000, f"distance {d:.0f}m outside expected range"
