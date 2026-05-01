"""Authentication helpers for the localhost web UI."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Request
from stravalib.client import Client

from ..onelap.auth import (
    DEFAULT_COOKIE_PATH,
    get_authenticated_onelap_client,
    load_cookie_jar,
    save_cookies_from_string,
)
from ..onelap.client import OnelapAuthRequired, OnelapError
from ..onelap.models import Activity
from ..strava_auth import (
    DEFAULT_TOKEN_PATH,
    SCOPES,
    StravaCredentials,
    Tokens,
    _load_tokens,
    _save_tokens,
    get_authenticated_client,
)


@dataclass
class ConnectionStatus:
    configured: bool
    authorized: bool
    ok: bool
    label: str
    detail: str | None = None
    expires_at: datetime | None = None
    saved_at: datetime | None = None


class OAuthStateStore:
    """Small in-memory state store for single-user localhost OAuth."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: set[str] = set()

    def issue(self) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            self._states.add(state)
        return state

    def consume(self, state: str | None) -> bool:
        if not state:
            return False
        with self._lock:
            if state not in self._states:
                return False
            self._states.remove(state)
            return True


def web_redirect_uri(request: Request) -> str:
    """Build the OAuth callback URL served by this web process.

    Strava Apps configured with callback domain ``localhost`` accept a
    localhost callback regardless of port. When the user opens the app via
    127.0.0.1, prefer ``localhost`` for the OAuth URL to match that guidance.
    """

    url = request.url_for("strava_callback")
    parsed = urlparse(str(url))
    if parsed.hostname in {"127.0.0.1", "0.0.0.0"}:
        netloc = f"localhost:{parsed.port}" if parsed.port else "localhost"
        return parsed._replace(netloc=netloc).geturl()
    return str(url)


def build_strava_authorization_url(
    creds: StravaCredentials, *, redirect_uri: str, state: str
) -> str:
    client = Client()
    return client.authorization_url(
        client_id=creds.client_id,
        redirect_uri=redirect_uri,
        scope=SCOPES,
        state=state,
    )


def exchange_strava_code(
    creds: StravaCredentials,
    code: str,
    *,
    token_path: Path = DEFAULT_TOKEN_PATH,
) -> Tokens:
    client = Client()
    access_info = client.exchange_code_for_token(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        code=code,
    )
    tokens = Tokens(
        access_token=access_info["access_token"],
        refresh_token=access_info["refresh_token"],
        expires_at=int(access_info["expires_at"]),
    )
    _save_tokens(tokens, token_path)
    return tokens


def strava_status(token_path: Path = DEFAULT_TOKEN_PATH) -> ConnectionStatus:
    try:
        StravaCredentials.from_env()
    except Exception as e:  # noqa: BLE001 - surfaced as user-facing setup state
        return ConnectionStatus(
            configured=False,
            authorized=False,
            ok=False,
            label="未配置",
            detail=str(e),
        )

    tokens = _load_tokens(token_path)
    if tokens is None:
        return ConnectionStatus(
            configured=True,
            authorized=False,
            ok=False,
            label="未授权",
            detail="尚未保存 Strava token。",
        )

    try:
        # Refresh if needed, then reload so the displayed expiry is current.
        get_authenticated_client(token_path=token_path, interactive=False)
        tokens = _load_tokens(token_path) or tokens
    except Exception as e:  # noqa: BLE001
        return ConnectionStatus(
            configured=True,
            authorized=False,
            ok=False,
            label="授权不可用",
            detail=str(e),
        )

    return ConnectionStatus(
        configured=True,
        authorized=True,
        ok=True,
        label="已授权",
        expires_at=datetime.fromtimestamp(tokens.expires_at, tz=timezone.utc),
    )


def save_and_verify_onelap_session(
    cookie: str,
    bearer: str | None,
    *,
    path: Path = DEFAULT_COOKIE_PATH,
) -> ConnectionStatus:
    jar = save_cookies_from_string(cookie, path, bearer=bearer)
    try:
        client = get_authenticated_onelap_client(path)
        activities = client.list_activities(limit=1)
    except OnelapAuthRequired as e:
        return ConnectionStatus(
            configured=True,
            authorized=False,
            ok=False,
            label="验证失败",
            detail=f"顽鹿登录态无效或已过期：{e}",
            saved_at=datetime.fromtimestamp(jar.saved_at, tz=timezone.utc),
        )
    except (OnelapError, Exception) as e:  # noqa: BLE001
        return ConnectionStatus(
            configured=True,
            authorized=False,
            ok=False,
            label="验证失败",
            detail=str(e),
            saved_at=datetime.fromtimestamp(jar.saved_at, tz=timezone.utc),
        )

    latest = _format_latest_activity(activities[0]) if activities else "账号暂无骑行记录"
    return ConnectionStatus(
        configured=True,
        authorized=True,
        ok=True,
        label="已验证",
        detail=latest,
        saved_at=datetime.fromtimestamp(jar.saved_at, tz=timezone.utc),
    )


def onelap_status(path: Path = DEFAULT_COOKIE_PATH) -> ConnectionStatus:
    jar = load_cookie_jar(path)
    if jar is None:
        return ConnectionStatus(
            configured=False,
            authorized=False,
            ok=False,
            label="未登录",
            detail="尚未保存顽鹿 Cookie / Bearer。",
        )

    try:
        client = get_authenticated_onelap_client(path)
        activities = client.list_activities(limit=1)
    except OnelapAuthRequired as e:
        return ConnectionStatus(
            configured=True,
            authorized=False,
            ok=False,
            label="登录态过期",
            detail=str(e),
            saved_at=datetime.fromtimestamp(jar.saved_at, tz=timezone.utc),
        )
    except (OnelapError, Exception) as e:  # noqa: BLE001
        return ConnectionStatus(
            configured=True,
            authorized=False,
            ok=False,
            label="验证失败",
            detail=str(e),
            saved_at=datetime.fromtimestamp(jar.saved_at, tz=timezone.utc),
        )

    latest = _format_latest_activity(activities[0]) if activities else "账号暂无骑行记录"
    return ConnectionStatus(
        configured=True,
        authorized=True,
        ok=True,
        label="已验证",
        detail=latest,
        saved_at=datetime.fromtimestamp(jar.saved_at, tz=timezone.utc),
    )


def _format_latest_activity(activity: Activity) -> str:
    start = activity.created_at_utc.astimezone().strftime("%Y-%m-%d %H:%M")
    distance_km = activity.distance_m / 1000.0
    return (
        f"最近一次骑行：{start}    "
        f"距离 {distance_km:.1f} km    "
        f"爬升 {activity.elevation_m:.0f} m"
    )

