"""HTTP client for Onelap's web-app private endpoints.

Everything that talks to onelap.cn goes through ``OnelapClient``. The
rest of the codebase treats it as an opaque facade returning typed
``Activity`` objects; that lets us migrate endpoints / signing schemes
without touching callers.

Authentication model is "pass-through cookies" (see
``contexts/phase2-onelap-api.md`` for why). The caller either:

- constructs the client directly with a cookie dict (useful for tests), or
- goes through ``onelap.auth.get_authenticated_onelap_client()`` which
  loads persisted cookies from disk.

Cookie expiry manifests as a *200 response with HTML body* (Onelap
redirects stale sessions to login HTML). We detect that and raise
``OnelapAuthRequired`` so the CLI can prompt for a fresh cookie dump.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from .models import Activity

logger = logging.getLogger(__name__)

BASE_URL_U = "http://u.onelap.cn"
PATH_LIST = "/analysis/list"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 20.0


class OnelapError(RuntimeError):
    """Base error for anything going wrong at the Onelap layer."""


class OnelapAuthRequired(OnelapError):
    """Cookies are missing / expired. Caller should prompt re-login."""


@dataclass
class DownloadedFit:
    """Result of ``download_fit``: where the file landed, plus what we know about it."""

    path: Path
    filename: str
    size_bytes: int


def _sanitize_filename(name: str) -> str:
    """Strip unsafe path characters so we can write to ``data/cache`` safely."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip().strip(".")
    return name or "activity.fit"


class OnelapClient:
    """Thin wrapper around ``requests.Session`` with Onelap-specific endpoints."""

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
    ) -> None:
        if not cookies:
            raise OnelapAuthRequired(
                "No Onelap cookies provided. Run `onelap2strava onelap-login` first."
            )
        self._cookies = dict(cookies)
        self._session = session or requests.Session()
        # Pre-populate the session cookie jar so retries / redirects carry auth.
        self._session.cookies.update(self._cookies)
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                # Mimic a real browser enough that Onelap's backend doesn't
                # bucket us as a bot and serve a login page.
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def _get(self, url: str, *, stream: bool = False) -> requests.Response:
        """GET with consistent timeout + error translation."""
        try:
            resp = self._session.get(url, timeout=HTTP_TIMEOUT_S, stream=stream)
        except requests.RequestException as e:
            raise OnelapError(f"network error calling {url}: {e}") from e
        return resp

    def list_activities(self, *, limit: int | None = None) -> list[Activity]:
        """Fetch activities from ``/analysis/list``, newest first.

        Onelap returns the full visible history in one shot (no pagination
        exposed in the reverse-engineered schema). We just cap client-side.
        """
        resp = self._get(BASE_URL_U + PATH_LIST)
        self._raise_if_auth_required(resp)
        if resp.status_code >= 400:
            raise OnelapError(
                f"GET {PATH_LIST} failed: {resp.status_code} {resp.reason}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            # 200 with non-JSON body == session expired on Onelap's side
            # (they serve the login HTML page). Translate to auth error.
            raise OnelapAuthRequired(
                "Onelap /analysis/list returned non-JSON; cookies likely expired. "
                "Run `onelap2strava onelap-login` with a fresh cookie string."
            ) from e

        items: Iterable[dict[str, Any]] = payload.get("data") or []
        activities = [Activity.from_api(x) for x in items]
        activities.sort(key=lambda a: a.created_at_utc, reverse=True)
        if limit is not None:
            activities = activities[:limit]
        return activities

    def download_fit(self, activity: Activity, cache_dir: Path) -> DownloadedFit:
        """Stream the FIT file to ``cache_dir/<filename>`` and return metadata.

        Idempotent: if the target file already exists with non-zero size, we
        treat it as a cache hit and skip re-downloading.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        url = activity.download_path
        if not url.startswith("http"):
            url = BASE_URL_U + url

        filename = _sanitize_filename(
            activity.filename_hint or f"{activity.activity_id}.fit"
        )
        if not filename.lower().endswith(".fit"):
            filename = f"{filename}.fit"
        out_path = cache_dir / filename

        if out_path.exists() and out_path.stat().st_size > 0:
            logger.info("cache hit: %s", out_path)
            return DownloadedFit(
                path=out_path, filename=filename, size_bytes=out_path.stat().st_size
            )

        resp = self._get(url, stream=True)
        self._raise_if_auth_required(resp)
        if resp.status_code >= 400:
            raise OnelapError(
                f"GET {url} failed: {resp.status_code} {resp.reason}"
            )

        # Use a temp file to avoid leaving a truncated .fit on crash.
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        size = 0
        try:
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)
            tmp_path.replace(out_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        return DownloadedFit(path=out_path, filename=filename, size_bytes=size)

    @staticmethod
    def _raise_if_auth_required(resp: requests.Response) -> None:
        """Translate Onelap's "session expired" signals into a specific error.

        Onelap is a mix of REST-ish JSON and classic server-rendered pages.
        When cookies go stale, `/analysis/*` does NOT return 401. Instead it
        redirects (or 200s) to an HTML login page. We catch both shapes.
        """
        if resp.status_code in (401, 403):
            raise OnelapAuthRequired(
                f"Onelap returned {resp.status_code}; cookies are invalid. "
                "Run `onelap2strava onelap-login` again."
            )
        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and "text/html" in ctype:
            raise OnelapAuthRequired(
                "Onelap returned an HTML page instead of JSON (session expired). "
                "Run `onelap2strava onelap-login` with a fresh cookie string."
            )
