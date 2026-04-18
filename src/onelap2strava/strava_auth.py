"""Strava OAuth 2.0 flow for a local single-user CLI.

On first run: spin up a one-shot HTTP server on ``localhost:8000/callback``,
open the browser to Strava's authorization page, receive the ``code``, trade
it for an access/refresh token, and persist to ``data/.strava_token.json``.

On subsequent runs: load tokens from disk. If the access token is expired
(Strava tokens live 6 hours), refresh using the refresh token. The refreshed
tokens are written back transparently.
"""

from __future__ import annotations

import json
import logging
import os
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from stravalib.client import Client

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_PATH = Path("data/.strava_token.json")
DEFAULT_REDIRECT_URI = "http://localhost:8000/callback"
SCOPES = ["activity:write", "activity:read"]
# Refresh when the access token is within this many seconds of expiring.
REFRESH_LEEWAY_SECONDS = 120


@dataclass
class StravaCredentials:
    """Strava OAuth app credentials loaded from environment / .env."""

    client_id: int
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> "StravaCredentials":
        load_dotenv()
        client_id_raw = os.environ.get("STRAVA_CLIENT_ID")
        client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
        redirect_uri = os.environ.get("STRAVA_REDIRECT_URI", DEFAULT_REDIRECT_URI)
        if not client_id_raw or not client_secret:
            raise RuntimeError(
                "STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET missing. "
                "Copy .env.example to .env and fill in the values from "
                "https://www.strava.com/settings/api."
            )
        try:
            client_id = int(client_id_raw)
        except ValueError as e:
            raise RuntimeError(f"STRAVA_CLIENT_ID must be an integer, got {client_id_raw!r}") from e
        return cls(client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)


@dataclass
class Tokens:
    """Persisted token state."""

    access_token: str
    refresh_token: str
    expires_at: int  # unix seconds

    def is_fresh(self, leeway: int = REFRESH_LEEWAY_SECONDS) -> bool:
        return time.time() + leeway < self.expires_at

    def to_json(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Tokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=int(data["expires_at"]),
        )


def _save_tokens(tokens: Tokens, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens.to_json(), indent=2), encoding="utf-8")


def _load_tokens(path: Path) -> Tokens | None:
    if not path.exists():
        return None
    try:
        return Tokens.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Could not parse %s (%s); will re-authorize.", path, e)
        return None


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures ``?code=...`` from Strava's redirect."""

    # Populated by the surrounding server; read by the driver after shutdown.
    result: dict = {}

    def do_GET(self):  # noqa: N802 (http.server naming convention)
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        if "error" in qs:
            _OAuthCallbackHandler.result = {"error": qs["error"][0]}
            body = f"<h2>Strava authorization failed: {qs['error'][0]}</h2>"
        elif "code" in qs:
            _OAuthCallbackHandler.result = {"code": qs["code"][0], "scope": qs.get("scope", [""])[0]}
            body = (
                "<h2>Authorization successful.</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
            )
        else:
            _OAuthCallbackHandler.result = {"error": "no code in callback"}
            body = "<h2>No authorization code in callback.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):  # noqa: A002  (silence default noisy logging)
        return


def _wait_for_callback(host: str, port: int, timeout_s: float) -> dict:
    """Run a one-shot HTTP server until a callback arrives or timeout hits."""
    _OAuthCallbackHandler.result = {}
    server = HTTPServer((host, port), _OAuthCallbackHandler)
    server.timeout = 0.5

    deadline = time.time() + timeout_s
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while time.time() < deadline and not _OAuthCallbackHandler.result:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
    return _OAuthCallbackHandler.result


def authorize(creds: StravaCredentials, token_path: Path = DEFAULT_TOKEN_PATH) -> Tokens:
    """Drive the full first-time authorization flow and return fresh tokens.

    Blocks until the user completes (or cancels) the Strava authorization.
    """
    client = Client()
    auth_url = client.authorization_url(
        client_id=creds.client_id,
        redirect_uri=creds.redirect_uri,
        scope=SCOPES,
    )
    parsed = urlparse(creds.redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8000

    print("Opening browser to Strava authorization page...")
    print(f"If the browser did not open, visit manually:\n  {auth_url}")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    result = _wait_for_callback(host, port, timeout_s=300)
    if not result:
        raise RuntimeError("Timed out waiting for Strava authorization callback.")
    if "error" in result:
        raise RuntimeError(f"Strava authorization failed: {result['error']}")

    code = result["code"]
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
    print(f"Authorized. Tokens saved to {token_path}.")
    return tokens


def _refresh(creds: StravaCredentials, tokens: Tokens, token_path: Path) -> Tokens:
    client = Client()
    access_info = client.refresh_access_token(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        refresh_token=tokens.refresh_token,
    )
    refreshed = Tokens(
        access_token=access_info["access_token"],
        refresh_token=access_info["refresh_token"],
        expires_at=int(access_info["expires_at"]),
    )
    _save_tokens(refreshed, token_path)
    return refreshed


def get_authenticated_client(
    token_path: Path = DEFAULT_TOKEN_PATH,
    *,
    interactive: bool = True,
) -> Client:
    """Return a ready-to-use ``stravalib.Client`` with a fresh access token.

    - Reads tokens from disk; if missing or parse-error, runs the full
      authorization flow (only when ``interactive=True``).
    - Auto-refreshes if near expiry.
    - Persists refreshed tokens for future runs.
    """
    creds = StravaCredentials.from_env()
    tokens = _load_tokens(token_path)
    if tokens is None:
        if not interactive:
            raise RuntimeError(
                f"No tokens found at {token_path}. Run `onelap2strava auth` first."
            )
        tokens = authorize(creds, token_path)
    elif not tokens.is_fresh():
        logger.info("Access token near expiry; refreshing.")
        tokens = _refresh(creds, tokens, token_path)

    client = Client(access_token=tokens.access_token)
    return client
