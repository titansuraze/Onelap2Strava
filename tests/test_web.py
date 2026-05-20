from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from onelap2strava.onelap.models import Activity
from onelap2strava.strava_auth import StravaCredentials, Tokens, _load_tokens
from onelap2strava.strava_client import UploadOutcome
from onelap2strava.sync import ActivitySyncResult, SyncReport
from onelap2strava.web.app import create_app
from onelap2strava.web.auth import (
    ConnectionStatus,
    build_strava_authorization_url,
    exchange_strava_code,
    save_and_verify_onelap_session,
)
from onelap2strava.web.sync_jobs import SyncJobManager


def _status(label: str, *, ok: bool = True) -> ConnectionStatus:
    return ConnectionStatus(
        configured=ok,
        authorized=ok,
        ok=ok,
        label=label,
        detail=None,
    )


def _activity(activity_id: str = "ride-1") -> Activity:
    return Activity(
        activity_id=activity_id,
        created_at_utc=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        distance_m=32100,
        elevation_m=456,
        download_path="/analysis/download/ride.fit",
        filename_hint="ride.fit",
        raw={},
    )


def test_index_renders_connection_statuses() -> None:
    activity = _activity()
    app = create_app(
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
        recent_activities_func=lambda limit: [
            {
                "activity": activity,
                "detail_url": "https://u.onelap.cn/recordPage/details?id=ride-1",
                "status": None,
                "status_label": "尚未上传",
                "can_sync": True,
            }
        ],
    )
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Strava ready" in response.text
    assert "Onelap ready" in response.text
    assert "批量同步数据" in response.text
    assert "同步最新数据" in response.text
    assert "近期运动数据" in response.text
    assert "尚未上传" in response.text
    assert "32.1 km" in response.text
    assert "https://u.onelap.cn/recordPage/details?id=ride-1" in response.text
    assert 'href="/sync"' not in response.text


def test_onelap_form_reports_parse_error() -> None:
    app = create_app(
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap missing", ok=False),
    )
    client = TestClient(app)

    response = client.post(
        "/onelap",
        data={"cookie": "not a cookie", "bearer": "authorization-token"},
    )

    assert response.status_code == 400
    assert "Could not parse any cookies" in response.text


def test_auth_page_combines_strava_and_onelap() -> None:
    app = create_app(
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
    )
    client = TestClient(app)

    response = client.get("/auth")

    assert response.status_code == 200
    assert "Strava ready" in response.text
    assert "Onelap ready" in response.text
    assert "Cookie" in response.text
    assert "Authorization" in response.text
    assert response.text.index("Authorization") < response.text.index("Cookie")


def test_legacy_auth_pages_redirect_to_combined_page() -> None:
    app = create_app(
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
    )
    client = TestClient(app)

    assert client.get("/strava", follow_redirects=False).headers["location"] == "/auth"
    assert client.get("/onelap", follow_redirects=False).headers["location"] == "/auth"


def test_onelap_form_requires_authorization() -> None:
    app = create_app(
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap missing", ok=False),
    )
    client = TestClient(app)

    response = client.post("/onelap", data={"cookie": "a=1", "bearer": ""})

    assert response.status_code == 400
    assert "Authorization" in response.text


def test_save_and_verify_onelap_session_uses_live_probe(tmp_path: Path, monkeypatch) -> None:
    class FakeClient:
        def list_activities(self, limit: int):
            assert limit == 1
            return [_activity()]

    monkeypatch.setattr(
        "onelap2strava.web.auth.get_authenticated_onelap_client",
        lambda path: FakeClient(),
    )

    status = save_and_verify_onelap_session(
        "OTOKEN=abc; other=1",
        "Bearer token",
        path=tmp_path / ".onelap_cookies.json",
    )

    assert status.ok is True
    assert status.label == "已验证"
    assert status.detail is not None
    assert status.detail.startswith("最近一次骑行：")
    assert "距离 32.1 km" in status.detail


def test_strava_authorization_url_helper(monkeypatch) -> None:
    class FakeClient:
        def authorization_url(self, **kwargs):
            assert kwargs["client_id"] == 123
            assert kwargs["redirect_uri"] == "http://localhost:8765/strava/callback"
            assert kwargs["state"] == "state-1"
            assert "activity:write" in kwargs["scope"]
            return "https://strava.example/auth"

    monkeypatch.setattr("onelap2strava.web.auth.Client", FakeClient)

    url = build_strava_authorization_url(
        StravaCredentials(
            client_id=123,
            client_secret="secret",
            redirect_uri="http://localhost:8000/callback",
        ),
        redirect_uri="http://localhost:8765/strava/callback",
        state="state-1",
    )

    assert url == "https://strava.example/auth"


def test_exchange_strava_code_writes_token(tmp_path: Path, monkeypatch) -> None:
    class FakeClient:
        def exchange_code_for_token(self, **kwargs):
            assert kwargs["client_id"] == 123
            assert kwargs["client_secret"] == "secret"
            assert kwargs["code"] == "code-1"
            return {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 1_900_000_000,
            }

    monkeypatch.setattr("onelap2strava.web.auth.Client", FakeClient)
    token_path = tmp_path / ".strava_token.json"

    tokens = exchange_strava_code(
        StravaCredentials(
            client_id=123,
            client_secret="secret",
            redirect_uri="http://localhost:8000/callback",
        ),
        "code-1",
        token_path=token_path,
    )

    assert tokens.access_token == "access"
    assert _load_tokens(token_path) == Tokens(
        access_token="access",
        refresh_token="refresh",
        expires_at=1_900_000_000,
    )


def test_sync_start_uses_incremental_runner() -> None:
    called: dict = {}

    def fake_runner(**kwargs):
        called.update(kwargs)
        return SyncReport()

    manager = SyncJobManager(
        runner=fake_runner,
        onelap_factory=lambda: SimpleNamespace(),
        strava_factory=lambda: SimpleNamespace(),
    )
    app = create_app(
        job_manager=manager,
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
    )
    client = TestClient(app)

    response = client.post("/sync/start", headers={"HX-Request": "true"})
    assert response.status_code == 200

    deadline = time.time() + 2
    while manager.snapshot().running and time.time() < deadline:
        time.sleep(0.01)

    assert called["incremental"] is True
    assert called["limit"] == 1
    assert called["force"] is False


def test_sync_start_redirects_back_home_without_hx() -> None:
    manager = SyncJobManager(
        runner=lambda **_: SyncReport(),
        onelap_factory=lambda: SimpleNamespace(),
        strava_factory=lambda: SimpleNamespace(),
    )
    app = create_app(
        job_manager=manager,
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
    )
    client = TestClient(app)

    response = client.post("/sync/start", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_sync_page_is_not_exposed() -> None:
    app = create_app(
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
    )
    client = TestClient(app)

    response = client.get("/sync")

    assert response.status_code == 404


def test_sync_start_latest_uses_n_one_runner() -> None:
    called: dict = {}

    def fake_runner(**kwargs):
        called.update(kwargs)
        return SyncReport()

    manager = SyncJobManager(
        runner=fake_runner,
        onelap_factory=lambda: SimpleNamespace(),
        strava_factory=lambda: SimpleNamespace(),
    )
    app = create_app(
        job_manager=manager,
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
        recent_activities_func=lambda limit: [],
    )
    client = TestClient(app)

    response = client.post(
        "/sync/start",
        data={"mode": "latest"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    deadline = time.time() + 2
    while manager.snapshot().running and time.time() < deadline:
        time.sleep(0.01)

    assert called["incremental"] is False
    assert called["limit"] == 1


def test_sync_activity_filters_to_selected_onelap_activity() -> None:
    called: dict = {}
    selected = _activity("ride-2")

    class FakeOnelap:
        def list_activities(self, limit=None):
            return [_activity("ride-1"), selected]

        def download_fit(self, activity, *, cache_dir):
            raise AssertionError("runner should not call download_fit in this test")

    def fake_runner(**kwargs):
        called.update(kwargs)
        assert kwargs["onelap"].list_activities() == [selected]
        return SyncReport()

    manager = SyncJobManager(
        runner=fake_runner,
        onelap_factory=FakeOnelap,
        strava_factory=lambda: SimpleNamespace(),
    )
    app = create_app(
        job_manager=manager,
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
        recent_activities_func=lambda limit: [],
    )
    client = TestClient(app)

    response = client.post("/sync/activity/ride-2", headers={"HX-Request": "true"})
    assert response.status_code == 200

    deadline = time.time() + 2
    while manager.snapshot().running and time.time() < deadline:
        time.sleep(0.01)

    assert called["incremental"] is False
    assert called["limit"] == 1


def test_sync_status_renders_finished_report() -> None:
    report = SyncReport(
        results=[
            ActivitySyncResult(
                activity=_activity(),
                uploaded=UploadOutcome(
                    skipped_duplicate=False,
                    existing_activity_id=None,
                    new_activity_id=123,
                    activity_url="https://www.strava.com/activities/123",
                    external_id="sha1:abc",
                ),
            )
        ]
    )
    manager = SyncJobManager(
        runner=lambda **_: report,
        onelap_factory=lambda: SimpleNamespace(),
        strava_factory=lambda: SimpleNamespace(),
    )
    assert manager.start_incremental() is True
    deadline = time.time() + 2
    while manager.snapshot().running and time.time() < deadline:
        time.sleep(0.01)

    app = create_app(
        job_manager=manager,
        strava_status_func=lambda: _status("Strava ready"),
        onelap_status_func=lambda: _status("Onelap ready"),
        recent_activities_func=lambda limit: [],
    )
    client = TestClient(app)

    response = client.get("/sync/status")

    assert response.status_code == 200
    assert "成功上传 1 条" in response.text
    assert "已上传到 Strava" in response.text
    assert "ok=" not in response.text

