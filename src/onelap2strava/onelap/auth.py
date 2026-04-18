"""Persist and load Onelap session cookies.

Because Onelap has no public login API we can confidently target yet
(see ``contexts/phase2-onelap-api.md``), the primary authentication
path is "**manual cookie import**":

1. User logs into onelap.cn in their browser.
2. User copies the ``Cookie`` header from DevTools.
3. ``onelap2strava onelap-login`` saves that string to
   ``data/.onelap_cookies.json``.

This is crude but bulletproof against login-endpoint changes. When the
login API is confirmed via packet capture, an ``api_login()`` call can
be added here without touching the rest of the codebase.

Cookies are stored as a JSON file (gitignored) rather than in the
system keyring because:

- Cookies are per-browser / per-device session artifacts, not long-lived
  credentials like passwords. Re-issuing them is trivial if leaked.
- Keyring-stored cookies would still need to be written to requests'
  session anyway; no security win for the added complexity.

If we ever also ask the user for the raw phone+password (to drive an
auto re-login), *those* go into keyring.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path

from .client import OnelapClient

logger = logging.getLogger(__name__)

DEFAULT_COOKIE_PATH = Path("data/.onelap_cookies.json")


class NotAuthenticatedError(RuntimeError):
    """Raised when no usable Onelap cookies are on disk."""


@dataclass
class CookieJar:
    """Persisted Onelap cookies + bookkeeping."""

    cookies: dict[str, str]
    saved_at: int  # unix seconds

    def to_json(self) -> dict:
        return {"cookies": self.cookies, "saved_at": self.saved_at}

    @classmethod
    def from_json(cls, data: dict) -> "CookieJar":
        return cls(
            cookies=dict(data["cookies"]),
            saved_at=int(data.get("saved_at", 0)),
        )


def _parse_cookie_header(raw: str) -> dict[str, str]:
    """Turn a raw ``Cookie: k1=v1; k2=v2`` header into a dict.

    Accepts the string with or without the leading ``Cookie:`` prefix,
    and trims whitespace; robust against the many ways users copy
    cookies out of DevTools.
    """
    s = raw.strip()
    if s.lower().startswith("cookie:"):
        s = s[len("cookie:") :].strip()

    # ``http.cookies.SimpleCookie`` handles quoting / escaping correctly.
    jar = SimpleCookie()
    jar.load(s)
    out: dict[str, str] = {name: morsel.value for name, morsel in jar.items()}

    if not out:
        # Fallback: some cookie exports use newlines instead of ``; ``.
        for part in s.replace("\n", ";").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()

    if not out:
        raise ValueError(
            "Could not parse any cookies from input. Expected a string like "
            "`PHPSESSID=abc; access_token=xyz`."
        )
    return out


def save_cookies(
    cookies: dict[str, str], path: Path = DEFAULT_COOKIE_PATH
) -> CookieJar:
    """Persist an already-parsed cookie dict to disk.

    Shared sink for both manual paste (``save_cookies_from_string``) and
    browser-auto-import (``browser_cookies.load_onelap_cookies_from_browser``)
    entry paths.
    """
    if not cookies:
        raise ValueError("Refusing to save an empty cookie dict.")
    jar = CookieJar(cookies=dict(cookies), saved_at=int(time.time()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jar.to_json(), indent=2), encoding="utf-8")
    logger.info("Onelap cookies saved to %s (%d entries)", path, len(cookies))
    return jar


def save_cookies_from_string(
    raw_cookie_string: str, path: Path = DEFAULT_COOKIE_PATH
) -> CookieJar:
    """Parse + persist a ``Cookie:`` header string to disk.

    Returns the stored :class:`CookieJar` for caller feedback.
    """
    cookies = _parse_cookie_header(raw_cookie_string)
    return save_cookies(cookies, path)


def load_cookie_jar(path: Path = DEFAULT_COOKIE_PATH) -> CookieJar | None:
    """Read the cookie jar from disk, or ``None`` if missing / malformed."""
    if not path.exists():
        return None
    try:
        return CookieJar.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Could not parse %s (%s); treat as unauthenticated.", path, e)
        return None


def get_authenticated_onelap_client(
    path: Path = DEFAULT_COOKIE_PATH,
) -> OnelapClient:
    """Return a ready-to-use :class:`OnelapClient` or raise if no cookies exist."""
    jar = load_cookie_jar(path)
    if jar is None:
        raise NotAuthenticatedError(
            f"No Onelap cookies found at {path}. "
            "Run `onelap2strava onelap-login` first."
        )
    return OnelapClient(cookies=jar.cookies)
