"""Unit tests for the Onelap interface layer.

We mock HTTP with the ``responses`` library (pure-Python, no sockets)
because the real Onelap endpoints are private and region-locked. The
fixture payloads here mirror the shape we verified from the reference
project + CSDN reports (see contexts/phase2-onelap-api.md).
"""

from __future__ import annotations

import base64
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
    LIST_ACTIVITY_GET_URLS,
    PATH_OTM_FIT_CONTENT,
    OnelapAuthRequired,
    OnelapClient,
    OnelapError,
    _fit_basename_from_durl,
)

# list_activities 优先打到的探测 URL（与 client 中候选第 1 个一致）
LIST_PROBE_URL = LIST_ACTIVITY_GET_URLS[0]
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


def _otm_urls_for_filekey(file_key: str) -> tuple[str, str]:
    b64 = base64.b64encode(file_key.encode("utf-8")).decode("ascii")
    path = f"{PATH_OTM_FIT_CONTENT}{b64}"
    return f"https://u.onelap.cn{path}", f"http://u.onelap.cn{path}"


def test_fit_basename_from_durl_extracts_rfsrv_filename() -> None:
    u = (
        "http://fits.rfsvr.net/geo/20260423/"
        "MAGENE_C506_1776953439_1338356_1776957987581.fit?e=1&token=x"
    )
    assert _fit_basename_from_durl(u) == (
        "MAGENE_C506_1776953439_1338356_1776957987581.fit"
    )


def test_download_candidates_add_filekey_geo_path_and_quoted_form() -> None:
    item = {
        "id": 9,
        "created_at": 1_000_000_000,
        "totalDistance": 1,
        "elevation": 0,
        "durl": "http://x.example/ignored.fit",
        "fileKey": "geo/20260101/MAGENE_foo.fit",
    }
    a = Activity.from_api(item)
    urls = _client()._download_url_candidates(a)
    otm_https, otm_http = _otm_urls_for_filekey("geo/20260101/MAGENE_foo.fit")
    assert otm_https in urls
    assert otm_http in urls
    assert f"{BASE_URL_U}/analysis/download/geo/20260101/MAGENE_foo.fit" in urls
    assert f"{BASE_URL_U}/analysis/download/geo%2F20260101%2FMAGENE_foo.fit" in urls


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


def test_bearer_in_cookie_jar_json_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    j = save_cookies_from_string("a=1", path, bearer="eyJhbGciOiJIUzI1")
    assert j.bearer == "eyJhbGciOiJIUzI1"
    j2 = load_cookie_jar(path)
    assert j2 is not None and j2.bearer == j.bearer


def test_save_cookies_refresher_preserves_bearer(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    save_cookies_from_string("a=1", path, bearer="keepme")
    save_cookies_from_string("a=2", path)  # no --bearer
    loaded = load_cookie_jar(path)
    assert loaded is not None
    assert loaded.cookies == {"a": "2"}
    assert loaded.bearer == "keepme"


def test_load_cookie_jar_missing_returns_none(tmp_path: Path) -> None:
    assert load_cookie_jar(tmp_path / "nope.json") is None


# ---------- list_activities ----------


@responses.activate
def test_list_activities_parses_and_sorts_newest_first() -> None:
    responses.add(
        responses.POST,
        LIST_PROBE_URL,
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
        responses.POST,
        LIST_PROBE_URL,
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
        responses.POST,
        LIST_PROBE_URL,
        body="<html><body>please log in</body></html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(OnelapAuthRequired):
        _client().list_activities()


@responses.activate
def test_list_activities_401_raises_auth_required() -> None:
    responses.add(
        responses.POST,
        LIST_PROBE_URL,
        body="nope",
        status=401,
    )
    with pytest.raises(OnelapAuthRequired):
        _client().list_activities()


@responses.activate
def test_list_activities_500_raises_generic_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只探测单一 URL，否则 client 会尝试候选列表中后续未 mock 的地址
    monkeypatch.setenv("ONELAP_LIST_URL", LIST_PROBE_URL)
    responses.add(
        responses.POST,
        LIST_PROBE_URL,
        body="server go boom",
        status=500,
    )
    # ride_record/list 在多次 POST 后会回退到 GET
    responses.add(
        responses.GET,
        LIST_PROBE_URL,
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
    otm_https, otm_http = _otm_urls_for_filekey("MAGENE_C506_NEW.fit")
    responses.add(responses.GET, otm_https, status=404)
    responses.add(responses.GET, otm_http, status=404)
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
    otm_https, otm_http = _otm_urls_for_filekey("MAGENE_C506_NEW.fit")
    responses.add(responses.GET, otm_https, status=404)
    responses.add(responses.GET, otm_http, status=404)
    responses.add(
        responses.GET,
        BASE_URL_U + "/analysis/download/NEW.fit",
        body="<html>login</html>",
        status=200,
        content_type="text/html",
    )
    with pytest.raises(OnelapAuthRequired):
        _client().download_fit(activity, cache_dir=tmp_path)


@responses.activate
def test_download_fit_retries_once_on_404_with_refreshed_durl(tmp_path: Path) -> None:
    """Signed CDN links can 404; re-listing may return a fresh durl (same activity id)."""
    stale = "http://old.example.com/stale.fit"
    fresh = "http://new.example.com/fresh.fit"
    item = {
        "id": 1002,
        "created_at": 1_712_500_000,
        "totalDistance": 32_170,
        "elevation": 150,
        "durl": stale,
        "fileKey": "MAGENE_C506_NEW.fit",
    }
    activity = Activity.from_api(item)
    list_payload = {
        "data": [
            {
                **item,
                "durl": fresh,
            }
        ]
    }
    payload = b"FIT\x00" + b"Y" * 128
    otm_https, otm_http = _otm_urls_for_filekey("MAGENE_C506_NEW.fit")
    responses.add(responses.GET, otm_https, status=404)
    responses.add(responses.GET, otm_http, status=404)
    responses.add(responses.GET, stale, status=404)
    responses.add(
        responses.GET,
        BASE_URL_U + "/analysis/download/stale.fit",
        status=404,
    )
    responses.add(
        responses.GET,
        BASE_URL_U + "/analysis/download/MAGENE_C506_NEW.fit",
        status=404,
    )
    responses.add(
        responses.GET,
        BASE_URL_U + "/analysis/download/1002.fit",
        status=404,
    )
    responses.add(
        responses.POST,
        LIST_PROBE_URL,
        json=list_payload,
        status=200,
    )
    responses.add(
        responses.GET,
        fresh,
        body=payload,
        status=200,
        content_type="application/octet-stream",
    )

    result = _client().download_fit(activity, cache_dir=tmp_path)
    assert result.path.read_bytes() == payload
    assert result.size_bytes == len(payload)


@responses.activate
def test_download_fit_falls_back_to_analysis_download_path(tmp_path: Path) -> None:
    """When primary durl 404, ``/analysis/download/<fileKey>`` may still work."""
    # 路径中不要用 ``*.fit``，否则会多出一个 basename 代理候选、需额外 mock
    bad = "http://bad.example.com/activity/123"
    proxy = BASE_URL_U + "/analysis/download/MAGENE_PROXY.fit"
    item = {
        "id": 2002,
        "created_at": 1_712_500_000,
        "totalDistance": 10_000,
        "elevation": 1,
        "durl": bad,
        "fileKey": "MAGENE_PROXY.fit",
    }
    activity = Activity.from_api(item)
    body = b"PROXY\x00" + b"z" * 64
    otm_https, otm_http = _otm_urls_for_filekey("MAGENE_PROXY.fit")
    responses.add(responses.GET, otm_https, status=404)
    responses.add(responses.GET, otm_http, status=404)
    responses.add(responses.GET, bad, status=404)
    responses.add(
        responses.GET,
        proxy,
        body=body,
        status=200,
        content_type="application/octet-stream",
    )

    result = _client().download_fit(activity, cache_dir=tmp_path)
    assert result.path.read_bytes() == body


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
                "created_at": 1_712_000_000,
                "totalDistance": 100,
                "elevation": 0,
            }
        )


def test_activity_from_api_otm_list_summary_without_durl() -> None:
    a = Activity.from_api(
        {
            "id": "69eb2889c98cca9e85068163",
            "created_at": "1970-01-01 08:00:00",
            "start_riding_time": "2026-04-24 14:20:24",
            "distance_km": 17.28,
            "time_seconds": 2957,
        }
    )
    assert a.activity_id == "69eb2889c98cca9e85068163"
    assert a.raw.get("_id") == "69eb2889c98cca9e85068163"
    assert a.distance_m == pytest.approx(17_280.0)
    assert a.created_at_utc.year == 2026
    assert "pending-list-summary" in a.download_path


def test_activity_short_description_is_stable() -> None:
    a = Activity.from_api(SAMPLE_ACTIVITIES["data"][1])
    s = a.short_description()
    assert "km" in s
    assert "1002" in s
