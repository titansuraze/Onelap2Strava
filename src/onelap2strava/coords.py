"""GCJ-02 (Mars Coordinate System) <-> WGS-84 conversion.

Chinese fitness apps (including Onelap) store GPS tracks in GCJ-02 for
regulatory compliance. Strava expects WGS-84. Converting GCJ-02 back to
WGS-84 is not a closed-form operation, so we iterate.

Reference: the forward transform is widely published under the name
"eviltransform"; the inverse is a fixed-point iteration on that transform.
"""

from __future__ import annotations

import math

# Earth params used by the official GCJ-02 algorithm.
_A = 6378245.0  # semi-major axis (meters), Krassovsky 1940 ellipsoid
_EE = 0.00669342162296594323  # eccentricity squared


def in_china(lat: float, lng: float) -> bool:
    """Rough bounding box covering greater China.

    GCJ-02 bias is applied by Chinese providers inside this region. Points
    clearly overseas (Europe, Americas, most of Asia) are passed through
    unchanged to avoid introducing a synthetic offset.

    NOTE: the box technically includes Hong Kong / Macau / Taiwan where in
    practice providers often do NOT bias. For Onelap's use case (rides in
    mainland China) this is the safer default; if you ever record rides in
    those regions, inspect your Fit before using this tool.
    """
    return 72.004 < lng < 137.8347 and 0.8293 < lat < 55.8271


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _delta(lat: float, lng: float) -> tuple[float, float]:
    """Compute (dLat, dLng) offsets applied by GCJ-02 at the given WGS-84 point."""
    d_lat = _transform_lat(lng - 105.0, lat - 35.0)
    d_lng = _transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1.0 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_A * (1.0 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lng = (d_lng * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return d_lat, d_lng


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """Forward transform: real-world WGS-84 -> China-biased GCJ-02."""
    if not in_china(lat, lng):
        return lat, lng
    d_lat, d_lng = _delta(lat, lng)
    return lat + d_lat, lng + d_lng


def gcj02_to_wgs84(lat: float, lng: float, iterations: int = 5) -> tuple[float, float]:
    """Inverse transform via fixed-point iteration.

    Given a GCJ-02 point, we want WGS such that ``wgs84_to_gcj02(wgs) == gcj``.
    Starting from ``wgs = gcj`` and repeatedly subtracting ``_delta(wgs)`` from
    ``gcj`` converges to sub-millimeter accuracy in 3-5 iterations because the
    delta function is Lipschitz with small constant across the Chinese region.
    """
    if not in_china(lat, lng):
        return lat, lng
    wgs_lat, wgs_lng = lat, lng
    for _ in range(iterations):
        d_lat, d_lng = _delta(wgs_lat, wgs_lng)
        wgs_lat = lat - d_lat
        wgs_lng = lng - d_lng
    return wgs_lat, wgs_lng


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in meters between two WGS-84 points."""
    r = 6371008.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
