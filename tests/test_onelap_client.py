"""Unit tests for the Onelap interface layer.

We mock HTTP with the ``responses`` library (pure-Python, no sockets)
because the real Onelap endpoints are private and region-locked. The
fixture payloads here mirror the shape we verified from the reference
project + CSDN reports (see contexts/phase2-onelap-api.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

from onelap2strava.onelap.auth import (
    CookieJar,
    _parse_cookie_header,
    load_cookie_jar,
    save_cookies_from_string,
)
from onelap2strava.onelap.client import (
    BASE_URL_U,
    PATH_LIST,
    OnelapAuthRequired,
    OnelapClient,
    OnelapError,
)
from onelap2strava.onelap.models import Activity


SAMPLE_ACTIVITIES = {
    "data": [
        {
            "id": 1001,
            "created_at": 1_712_400_000,  # older
            "totalDistance": 15_000,
            "elevation": 80,
            "durl": "/analysis/download/OLD.fit",
            "fileKey": "MAGENE_OLD.fit",
        },
        {
            "id": 1002,
            "created_at": 1_712_500_000,  # newer
            "totalDistance": 32_170,
            "elevation": 150,
            "durl": "/analysis/download/NEW.fit",
            "fileKey": "MAGENE_C506_NEW.fit",
        },
    ]
}


def _client() -> OnelapClient:
    return OnelapClient(cookies={"PHPSESSID": "fake", "access_token": "t0ken"})


# ---------- cookie parsing ----------


def test_parse_cookie_header_basic() -> None:
    out = _parse_cookie_header("PHPSESSID=abc; access_token=xyz")
    assert out == {"PHPSESSID": "abc", "access_token": "xyz"}


def test_parse_cookie_header_tolerates_prefix_and_whitespace() -> None:
    out = _parse_cookie_header("  Cookie: a=1;  b=two ")
    assert out == {"a": "1", "b": "two"}


def test_parse_cookie_header_rejects_empty() -> None:
    with pytest.raises(ValueError):
        _parse_cookie_header("")


def test_save_and_load_cookie_jar_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "cookies.json"
    jar = save_cookies_from_string("k=v", path)
    assert jar.cookies == {"k": "v"}
    assert path.exists()

    loaded = load_cookie_jar(path)
    assert loaded is not None
    assert loaded.cookies == {"k": "v"}


def test_load_cookie_jar_missing_returns_none(tmp_path: Path) -> None:
    assert load_cookie_jar(tmp_path / "nope.json") is None


# ---------- list_activities ----------


@responses.activate
def test_list_activities_parses_and_sorts_newest_first() -> None:
    responses.add(
        responses.GET,
        BASE_URL_U + PATH_LIST,
        json=SAMPLE_ACTIVITIES,
        status=200,
    )
    activities = _client().list_activities()
    assert len(activities) == 2
    # Newest (1002) should come first.
    assert activities[0].activity_id == "1002"
    assert activities[0].distance_m == 32_170
    assert activities[0].download_path == "/analysis/download/NEW.fit"
    assert activities[0].filename_hint == "MAGENE_C506_NEW.fit"
    # created_at is parsed to UTC datetime.
    assert activities[0].created_at_utc.tzinfo is not None


@responses.activate
def test_list_activities_respects_limit() -> None:
    responses.add(
        responses.GET,
        BASE_URL_U + PATH_LIST,
        json=SAMPLE_ACTIVITIES,
        status=200,
    )
    activities = _client().list_activities(limit=1)
    assert len(activities) == 1
    assert activities[0].activity_id == "1002"


@responses.activate
def test_list_activities_html_response_raises_auth_required() -> None:
    """Onelap serves the login HTML page when cookies are stale."""
    responses.add(
        responses.GET,
        BASE_URL_U + PATH_LIST,
        body="<html><body>please log in</body></html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(OnelapAuthRequired):
        _client().list_activities()


@responses.activate
def test_list_activities_401_raises_auth_required() -> None:
    responses.add(
        responses.GET,
        BASE_URL_U + PATH_LIST,
        body="nope",
        status=401,
    )
    with pytest.raises(OnelapAuthRequired):
        _client().list_activities()


@responses.activate
def test_list_activities_500_raises_generic_error() -> None:
    responses.add(
        responses.GET,
        BASE_URL_U + PATH_LIST,
        body="server go boom",
        status=500,
    )
    with pytest.raises(OnelapError):
        _client().list_activities()


def test_client_rejects_empty_cookies() -> None:
    with pytest.raises(OnelapAuthRequired):
        OnelapClient(cookies={})


# ---------- download_fit ----------


@responses.activate
def test_download_fit_streams_to_cache_dir(tmp_path: Path) -> None:
    activity = Activity.from_api(SAMPLE_ACTIVITIES["data"][1])
    payload = b"FIT\x00" + b"X" * 1024
    responses.add(
        responses.GET,
        BASE_URL_U + "/analysis/download/NEW.fit",
        body=payload,
        status=200,
        content_type="application/octet-stream",
    )

    result = _client().download_fit(activity, cache_dir=tmp_path)
    assert result.path.exists()
    assert result.path.read_bytes() == payload
    assert result.size_bytes == len(payload)
    assert result.filename.endswith(".fit")


@responses.activate
def test_download_fit_cache_hit_skips_network(tmp_path: Path) -> None:
    activity = Activity.from_api(SAMPLE_ACTIVITIES["data"][1])
    # Pre-seed cache with a non-empty file.
    cached = tmp_path / "MAGENE_C506_NEW.fit"
    cached.write_bytes(b"cached-bytes")

    # Register NO responses; if the client tries to fetch, ``responses``
    # will raise ConnectionError on an unmatched request.
    result = _client().download_fit(activity, cache_dir=tmp_path)
    assert result.path == cached
    assert result.path.read_bytes() == b"cached-bytes"


@responses.activate
def test_download_fit_session_expired_raises_auth(tmp_path: Path) -> None:
    activity = Activity.from_api(SAMPLE_ACTIVITIES["data"][1])
    responses.add(
        responses.GET,
        BASE_URL_U + "/analysis/download/NEW.fit",
        body="<html>login</html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(OnelapAuthRequired):
        _client().download_fit(activity, cache_dir=tmp_path)


# ---------- Activity.from_api ----------


def test_activity_from_api_handles_iso_string_created_at() -> None:
    a = Activity.from_api(
        {
            "id": "xyz",
            "created_at": "2026-04-06T15:17:26Z",
            "totalDistance": 1000,
            "elevation": 10,
            "durl": "/analysis/download/abc.fit",
            "fileKey": "abc.fit",
        }
    )
    assert a.created_at_utc.year == 2026


def test_activity_from_api_rejects_missing_download() -> None:
    with pytest.raises(ValueError):
        Activity.from_api(
            {
                "id": 1,
                "created_at": 1_712_000_000,
                "totalDistance": 100,
                "elevation": 0,
            }
        )


def test_activity_short_description_is_stable() -> None:
    a = Activity.from_api(SAMPLE_ACTIVITIES["data"][1])
    s = a.short_description()
    assert "km" in s
    assert "1002" in s
