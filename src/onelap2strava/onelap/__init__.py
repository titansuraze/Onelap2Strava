"""Onelap (顽鹿) private-API access layer.

Kept in its own package because Onelap has no public API and the private
endpoints may change without notice. Everything that talks to onelap.cn
lives here; the rest of the codebase (sync, cli) only depends on the
typed facade exposed by ``client`` and ``auth``.

See ``contexts/phase2-onelap-api.md`` for the known endpoint catalog and
recon guide.
"""

from .auth import (
    CookieJar,
    DEFAULT_COOKIE_PATH,
    NotAuthenticatedError,
    get_authenticated_onelap_client,
    save_cookies,
    save_cookies_from_string,
)
from .client import OnelapClient, OnelapError
from .models import Activity

__all__ = [
    "Activity",
    "CookieJar",
    "DEFAULT_COOKIE_PATH",
    "NotAuthenticatedError",
    "OnelapClient",
    "OnelapError",
    "get_authenticated_onelap_client",
    "save_cookies",
    "save_cookies_from_string",
]
