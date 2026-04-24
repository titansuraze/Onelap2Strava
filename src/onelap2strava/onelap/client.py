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

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlencode, urlparse

import requests

from .models import Activity

logger = logging.getLogger(__name__)

BASE_URL_U = "http://u.onelap.cn"
# 运动记录迁移至 ``/record`` 后，网页「下载」走 OTM，fileKey 路径做 Base64 后挂在此段路径下（2026-04 起）。
PATH_OTM_FIT_CONTENT = "/api/otm/ride_record/analysis/fit_content/"
# 列表无 fileKey 时用于补全（与 ``/record/details?id=`` 同一条记录）
OTM_RIDE_RECORD_DETAIL_URL = "https://u.onelap.cn/api/otm/ride_record/detail"
# 自 2026 起 ``/analysis/list`` 常改为 HTML/重定向；活动列表以 OTM 为优先。可用环境变量
# ONELAP_LIST_URL 指定单一地址覆盖以下候选（抓包自 Network 中返回 JSON 的那条请求）。
PATH_LIST = "/analysis/list"  # 遗留名；仍作候选之末，便于旧站兼容
LIST_ACTIVITY_GET_URLS: tuple[str, ...] = (
    "https://u.onelap.cn/api/otm/ride_record/list",
    "https://u.onelap.cn/api/otm/ride_record/record_list",
    "https://u.onelap.cn/api/otm/ride_record/records",
    "https://u.onelap.cn/api/otm/ride_record/analysis/list",
    f"https://u.onelap.cn{PATH_LIST}",  # 少数环境仍可能提供 JSON
    f"{BASE_URL_U}{PATH_LIST}",
)
# ``/record`` 前端对 ``ride_record/list`` 使用 POST + ``application/json`` 请求体；GET 往往无效。
# 与 ``/record`` 抓包一致：``{"page":1,"limit":20}``；同步时把 limit 抬高以减少分页往返。
OTM_RIDE_RECORD_LIST_POST_BODIES: tuple[dict[str, Any], ...] = (
    {"page": 1, "limit": 200},
    {"current": 1, "size": 200},
    {"page": 1, "pageSize": 200},
    {"pageIndex": 0, "pageSize": 200},
    {},
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 20.0


def _http_body_looks_like_html(resp: requests.Response) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "text/html" in ct:
        return True
    try:
        sample = (resp.text or "")[:500].lstrip()
    except Exception:
        return True
    return sample.startswith("<!") or sample.lower().startswith("<html")


def _extract_activity_list_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    c = payload.get("code")
    if c is not None and c not in (200, 0, "200", "0", "ok", True):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "rows", "items", "rideList", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    for key in ("list", "records"):
        v = payload.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _list_payload_success_empty(
    items: list[dict[str, Any]], body: dict[str, Any]
) -> bool:
    """无活动时 JSON 常仍为 ``{code:200,data:[]}`` 等合法形态。"""
    if items:
        return True
    c = body.get("code")
    if c is not None and c not in (200, 0, "200", "0", "ok", True):
        return False
    data = body.get("data")
    if isinstance(data, list) and len(data) == 0:
        return True
    if isinstance(data, dict):
        for key in ("list", "records", "rows"):
            v = data.get(key)
            if isinstance(v, list) and len(v) == 0:
                return True
    return False


def _list_request_urls() -> tuple[str, ...]:
    one = (os.environ.get("ONELAP_LIST_URL") or "").strip()
    if one:
        return (one,)
    return LIST_ACTIVITY_GET_URLS


def _is_otm_ride_record_list_url(url: str) -> bool:
    return urlparse(url).path.rstrip("/").endswith("/ride_record/list")


def _otm_json_detail_merge_dict(resp: requests.Response) -> dict[str, Any] | None:
    """Parse ``/ride_record/detail``-style body; return a dict to merge into list row."""
    try:
        j = resp.json()
    except ValueError:
        return None
    if not isinstance(j, dict):
        return None
    c = j.get("code")
    if c is not None and c not in (200, 0, "200", "0", "ok", True):
        return None
    data = j.get("data")
    if isinstance(data, dict) and (
        data.get("fileKey")
        or data.get("durl")
        or data.get("fitUrl")
    ):
        return data
    if j.get("fileKey") or j.get("durl") or j.get("fitUrl"):
        return {k: j[k] for k in j if k in ("fileKey", "durl", "fitUrl")}
    return None


def _list_http_attempts(
    list_url: str,
) -> list[tuple[str, dict[str, Any] | None]]:
    """(method, json_body) for list probe; body is set only for POST."""
    if _is_otm_ride_record_list_url(list_url):
        post = [("POST", b) for b in OTM_RIDE_RECORD_LIST_POST_BODIES]
        return post + [("GET", None)]
    return [("GET", None)]


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


def _fit_basename_from_durl(durl: str) -> str | None:
    """Last path segment ``*.fit`` from a full URL or relative ``/analysis/...`` path.

    顽鹿的 ``durl`` 常指向 ``fits.rfsvr.net/.../MAGENE_..._<id>_....fit``；代理下载则仍用
    同文件名 ``/analysis/download/<该文件名>``，而不是 ``<id>.fit``。
    """
    s = durl.strip()
    if not s:
        return None
    if s.startswith("http"):
        path = urlparse(s).path
    else:
        path = s.split("?")[0]
    base = unquote(path.rstrip("/").split("/")[-1])
    if base.lower().endswith(".fit"):
        return base
    return None


def _file_key_path_for_otm_api(row: dict[str, Any], activity: Activity) -> str | None:
    """``geo/.../x.fit`` (or ``x.fit``) for ``/api/otm/ride_record/analysis/fit_content/<b64>``."""
    fk = row.get("fileKey") or activity.filename_hint
    if fk and not str(fk).lower().startswith("http"):
        name = str(fk).lstrip("/")
        if not name.lower().endswith(".fit"):
            name = f"{name}.fit"
        return name
    for raw in (activity.download_path, row.get("durl"), row.get("fitUrl")):
        if not raw or not str(raw).strip().lower().startswith("http"):
            continue
        p = urlparse(str(raw).strip())
        parts = [x for x in p.path.split("/") if x]
        if "geo" in parts:
            i = parts.index("geo")
            return "/".join(parts[i:])
    return None


class OnelapClient:
    """Thin wrapper around ``requests.Session`` with Onelap-specific endpoints."""

    def __init__(
        self,
        cookies: dict[str, str],
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
        authorization_bearer: str | None = None,
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
        if authorization_bearer:
            self._session.headers["Authorization"] = (
                f"Bearer {authorization_bearer.strip()}"
            )

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def _get(
        self,
        url: str,
        *,
        stream: bool = False,
        activity: Activity | None = None,
    ) -> requests.Response:
        """GET with consistent timeout + error translation."""
        extra: dict[str, str] = {}
        # 顽鹿部分 FIT 托管在 rfsvr CDN；与浏览器行为对齐，部分环境无 Referer 会 404。
        if "fits.rfsvr.net" in url:
            extra["Referer"] = f"{BASE_URL_U}/analysis/"
        elif "u.onelap.cn" in url and "/analysis/download/" in url:
            extra["Referer"] = f"{BASE_URL_U}/analysis/"
        elif "u.onelap.cn" in url and PATH_OTM_FIT_CONTENT in url:
            # 与 Network 抓包一致：详情页下载时 Referer 为 ``/record/details?id=<_id>``
            rid: str | None = None
            if activity is not None:
                meta = activity.raw.get("_id") or activity.raw.get("id")
                if meta is not None:
                    rid = str(meta)
            if rid:
                extra["Referer"] = f"https://u.onelap.cn/record/details?id={rid}"
            else:
                extra["Referer"] = "https://u.onelap.cn/record/"
        elif "u.onelap.cn" in url and "/api/otm/ride_record/" in url:
            # 活动列表、其它 OTM 读接口（与 ``/record`` 前端一致）
            extra["Referer"] = "https://u.onelap.cn/record/"
        try:
            resp = self._session.get(
                url, timeout=HTTP_TIMEOUT_S, stream=stream, headers=extra or None
            )
        except requests.RequestException as e:
            raise OnelapError(f"network error calling {url}: {e}") from e
        return resp

    def _post_json(self, url: str, data: dict[str, Any]) -> requests.Response:
        """POST JSON; Used for OTM ``ride_record/list`` (``/record`` 前端为 POST)。"""
        extra: dict[str, str] = {}
        if "u.onelap.cn" in url and "/api/otm/ride_record/" in url:
            extra["Referer"] = "https://u.onelap.cn/record/"
        try:
            return self._session.post(
                url,
                json=data,
                timeout=HTTP_TIMEOUT_S,
                headers=extra or None,
            )
        except requests.RequestException as e:
            raise OnelapError(f"network error calling {url}: {e}") from e

    @staticmethod
    def _resolve_download_url(durl: str) -> str:
        if durl.startswith("http"):
            return durl
        return BASE_URL_U + durl

    def _download_url_candidates(self, activity: Activity) -> list[str]:
        """Ordered unique URLs to try; Onelap may list a dead rfsvr link while
        ``/analysis/download/...`` or ``fitUrl`` still works.
        """
        seen: set[str] = set()
        out: list[str] = []

        def add(raw: str | None) -> None:
            if raw is None:
                return
            s = str(raw).strip()
            if not s:
                return
            u = self._resolve_download_url(s)
            if u in seen:
                return
            seen.add(u)
            out.append(u)

        row = activity.raw
        fk_path = _file_key_path_for_otm_api(row, activity)
        if fk_path:
            b64 = base64.b64encode(fk_path.encode("utf-8")).decode("ascii")
            otm_suffix = f"{PATH_OTM_FIT_CONTENT}{b64}"
            add("https://u.onelap.cn" + otm_suffix)
            add("http://u.onelap.cn" + otm_suffix)

        add(activity.download_path)
        if row.get("durl") is not None:
            add(str(row["durl"]))
        if row.get("fitUrl") is not None:
            add(str(row["fitUrl"]))

        for u in list(out):
            if u.startswith("http://fits.rfsvr.net/"):
                add("https://" + u[len("http://") :])

        fk = activity.filename_hint or row.get("fileKey")
        if fk and not str(fk).lower().startswith("http"):
            name = str(fk).lstrip("/")
            if not name.lower().endswith(".fit"):
                name = f"{name}.fit"
            # 顽鹿的 fileKey 常带 ``geo/YYYYMMDD/...``，与 durl 路径一致，优先于「只取文件名」的代理路径。
            add(f"/analysis/download/{name}")
            if "/" in name:
                add(f"/analysis/download/{quote(name, safe='')}")

        for raw in (activity.download_path, row.get("durl"), row.get("fitUrl")):
            if raw is None:
                continue
            base = _fit_basename_from_durl(str(raw))
            if base:
                add(f"/analysis/download/{base}")

        add(f"/analysis/download/{activity.activity_id}.fit")
        return out

    def _refetch_activity_by_id(self, activity_id: str) -> Activity | None:
        """Re-list and find one activity, used to refresh a stale signed durl."""
        for a in self.list_activities():
            if a.activity_id == activity_id:
                return a
        return None

    def _otm_detail_dict_for_merge(self, record_id: str) -> dict[str, Any] | None:
        """Fetch record detail to obtain ``fileKey`` / ``durl`` when list is summary-only."""
        for body in ({"_id": record_id}, {"id": record_id}):
            try:
                r = self._post_json(OTM_RIDE_RECORD_DETAIL_URL, body)
            except OnelapError:
                continue
            if r.status_code != 200:
                continue
            out = _otm_json_detail_merge_dict(r)
            if out:
                return out
        for qs in ({"_id": record_id}, {"id": record_id}):
            try:
                u = f"{OTM_RIDE_RECORD_DETAIL_URL}?{urlencode(qs)}"
                r = self._get(u, stream=False)
            except OnelapError:
                continue
            if r.status_code != 200:
                continue
            out = _otm_json_detail_merge_dict(r)
            if out:
                return out
        return None

    def _enrich_with_otm_detail(self, activity: Activity) -> Activity:
        if _file_key_path_for_otm_api(activity.raw, activity) is not None:
            return activity
        rid = activity.raw.get("_id") or activity.raw.get("id")
        if not rid:
            return activity
        extra = self._otm_detail_dict_for_merge(str(rid))
        if not extra:
            return activity
        merged = {**activity.raw, **extra}
        try:
            return Activity.from_api(merged)
        except ValueError:
            logger.info("OTM detail merge did not match Activity; using list row")
            return activity

    def list_activities(self, *, limit: int | None = None) -> list[Activity]:
        """Fetch activities from the first working list endpoint, newest first.

        Tries OTM ``/api/otm/ride_record/...`` URLs (``/record`` 前端) first, then
        legacy ``/analysis/list``. Override with env ``ONELAP_LIST_URL`` if
        顽鹿再改版。
        """
        items: list[dict[str, Any]] = []
        last_error: str | None = None
        found = False
        for list_url in _list_request_urls():
            for method, pbody in _list_http_attempts(list_url):
                try:
                    if method == "POST" and pbody is not None:
                        resp = self._post_json(list_url, pbody)
                    else:
                        resp = self._get(list_url, stream=False)
                except OnelapError as e:
                    last_error = str(e)
                    continue
                self._raise_if_auth_required(resp)
                if resp.status_code >= 400:
                    if (
                        method == "POST"
                        and resp.status_code
                        in (400, 404, 405, 408, 409, 422, 415, 429)
                    ):
                        last_error = (
                            f"{method} {list_url!r} -> {resp.status_code} "
                            f"{resp.reason!r} (body={pbody!r})"
                        )
                        continue
                    last_error = (
                        f"{method} {list_url!r} -> {resp.status_code} {resp.reason!r}"
                    )
                    continue
                try:
                    jbody = resp.json()
                except ValueError as e:
                    if _http_body_looks_like_html(resp):
                        raise OnelapAuthRequired(
                            "Onelap list endpoint returned non-JSON (login HTML?). "
                            "Re-run `onelap2strava onelap-login` with fresh Cookie "
                            "and optional `--bearer`."
                        ) from e
                    last_error = f"non-JSON: {e!r}"
                    continue
                if not isinstance(jbody, dict):
                    last_error = "list payload is not a JSON object"
                    continue
                items = _extract_activity_list_items(jbody)
                if items or _list_payload_success_empty(items, jbody):
                    found = True
                    break
                last_error = f"unrecognized list shape (keys: {list(jbody)[:8]!r}…)"
            if found:
                break
        else:
            raise OnelapError(
                "Could not load the activity list from any known Onelap endpoint. "
                f"Last: {last_error!s}. Set env ONELAP_LIST_URL to the full URL of "
                "a Network request that returns your rides as JSON, then retry."
            )

        try:
            activities = [Activity.from_api(x) for x in items]
        except ValueError as e:
            raise OnelapError(
                f"Onelap list JSON shape looks wrong for this tool: {e}. "
                "Set ONELAP_LIST_URL to a different list request or report a sample."
            ) from e
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

        last_url = ""
        last_status = 0
        last_reason = ""
        resp: requests.Response | None = None
        for round_idx in range(2):
            ref: Activity | None = (
                activity
                if round_idx == 0
                else self._refetch_activity_by_id(activity.activity_id)
            )
            if ref is None:
                break
            if round_idx == 1:
                logger.info(
                    "re-listing for fresh download url (pass 2) for %s",
                    activity.activity_id,
                )
            ref = self._enrich_with_otm_detail(ref)
            candidates = self._download_url_candidates(ref)
            for url in candidates:
                last_url = url
                r = self._get(url, stream=True, activity=ref)
                self._raise_if_auth_required(r)
                last_status = r.status_code
                last_reason = r.reason
                if r.status_code == 404:
                    r.close()
                    continue
                if r.status_code >= 400:
                    r.close()
                    continue
                resp = r
                break
            if resp is not None:
                break

        if resp is None:
            if last_url:
                raise OnelapError(
                    f"GET {last_url} failed: {last_status} {last_reason}"
                )
            raise OnelapError(
                f"fit download failed for activity {activity.activity_id} "
                "(no candidates or re-list after first pass)"
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
