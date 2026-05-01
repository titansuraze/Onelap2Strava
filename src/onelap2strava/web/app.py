"""FastAPI app for the localhost Onelap2Strava web UI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..onelap.auth import get_authenticated_onelap_client
from ..onelap.client import OnelapAuthRequired, OnelapError
from ..sync_log import DEFAULT_DB_PATH, SyncLog
from ..strava_auth import StravaCredentials
from .auth import (
    ConnectionStatus,
    OAuthStateStore,
    build_strava_authorization_url,
    exchange_strava_code,
    onelap_status,
    save_and_verify_onelap_session,
    strava_status,
    web_redirect_uri,
)
from .sync_jobs import SyncJobManager

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _format_dt(value) -> str:
    if value is None:
        return "-"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["dt"] = _format_dt


def recent_sync_rows(limit: int = 10):
    if not DEFAULT_DB_PATH.exists():
        return []
    with SyncLog.open(DEFAULT_DB_PATH) as log:
        return log.recent(limit=limit)


def _sync_status_by_onelap_id() -> dict[str, str]:
    if not DEFAULT_DB_PATH.exists():
        return {}
    with SyncLog.open(DEFAULT_DB_PATH) as log:
        return {row.onelap_activity_id: row.status for row in log.recent(limit=500)}


def recent_onelap_activities(limit: int = 8) -> list[dict]:
    client = get_authenticated_onelap_client()
    statuses = _sync_status_by_onelap_id()
    rows = []
    for activity in client.list_activities(limit=limit):
        status = statuses.get(activity.activity_id)
        rows.append(
            {
                "activity": activity,
                "detail_url": _onelap_detail_url(activity),
                "status": status,
                "status_label": _activity_status_label(status),
                "can_sync": status is None or status == "failed",
            }
        )
    return rows


def _activity_status_label(status: str | None) -> str:
    if status is None:
        return "尚未上传"
    return {
        "ok": "已上传",
        "duplicate": "已跳过重复",
        "failed": "同步失败",
        "manual": "手动处理",
        "backfilled": "已回填",
    }.get(status, status)


def _onelap_detail_url(activity) -> str:
    raw_id = activity.raw.get("_id") or activity.raw.get("id") if activity.raw else None
    detail_id = raw_id or activity.activity_id
    return f"https://u.onelap.cn/recordPage/details?id={detail_id}"


def _redirect(path: str, **query: str) -> RedirectResponse:
    suffix = f"?{urlencode(query)}" if query else ""
    return RedirectResponse(f"{path}{suffix}", status_code=303)


def create_app(
    *,
    job_manager: SyncJobManager | None = None,
    strava_status_func: Callable[[], ConnectionStatus] = strava_status,
    onelap_status_func: Callable[[], ConnectionStatus] = onelap_status,
    recent_activities_func: Callable[[int], list[dict]] = recent_onelap_activities,
    oauth_states: OAuthStateStore | None = None,
) -> FastAPI:
    app = FastAPI(title="Onelap2Strava Local Web UI")
    app.mount(
        "/static",
        StaticFiles(directory=str(PACKAGE_DIR / "static")),
        name="static",
    )
    jobs = job_manager or SyncJobManager()
    states = oauth_states or OAuthStateStore()

    def common_context(request: Request) -> dict:
        return {
            "request": request,
            "strava": strava_status_func(),
            "onelap": onelap_status_func(),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        ctx = common_context(request)
        activity_error = None
        try:
            activities = recent_activities_func(8) if ctx["onelap"].ok else []
        except (OnelapAuthRequired, OnelapError, Exception) as e:  # noqa: BLE001
            activities = []
            activity_error = str(e)
        ctx.update(
            {
                "activities": activities,
                "activity_error": activity_error,
                "job": jobs.snapshot(),
            }
        )
        return templates.TemplateResponse(request, "index.html", ctx)

    @app.get("/strava", response_class=HTMLResponse)
    def strava_page(request: Request):
        return RedirectResponse("/auth", status_code=303)

    @app.get("/auth", response_class=HTMLResponse)
    def auth_page(request: Request):
        ctx = common_context(request)
        ctx["message"] = request.query_params.get("message")
        ctx["error"] = request.query_params.get("error")
        return templates.TemplateResponse(request, "auth.html", ctx)

    @app.get("/strava/authorize")
    def strava_authorize(request: Request):
        try:
            creds = StravaCredentials.from_env()
            state = states.issue()
            auth_url = build_strava_authorization_url(
                creds,
                redirect_uri=web_redirect_uri(request),
                state=state,
            )
        except Exception as e:  # noqa: BLE001
            return _redirect("/auth", error=str(e))
        return RedirectResponse(auth_url, status_code=303)

    @app.get("/strava/callback", name="strava_callback")
    def strava_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ):
        if error:
            return _redirect("/auth", error=f"Strava 授权失败：{error}")
        if not states.consume(state):
            return _redirect("/auth", error="OAuth state 校验失败，请重试。")
        if not code:
            return _redirect("/auth", error="Strava 回调缺少 code。")
        try:
            creds = StravaCredentials.from_env()
            exchange_strava_code(creds, code)
        except Exception as e:  # noqa: BLE001
            return _redirect("/auth", error=str(e))
        return _redirect("/auth", message="Strava 授权成功。")

    @app.get("/onelap", response_class=HTMLResponse)
    def onelap_page(request: Request):
        return RedirectResponse("/auth", status_code=303)

    @app.post("/onelap", response_class=HTMLResponse)
    def onelap_save(
        request: Request,
        cookie: Annotated[str, Form()],
        bearer: Annotated[str, Form()] = "",
    ):
        if not bearer.strip():
            ctx = common_context(request)
            ctx["error"] = "请粘贴 Request Headers 里的 Authorization。"
            return templates.TemplateResponse(request, "auth.html", ctx, status_code=400)
        try:
            status = save_and_verify_onelap_session(cookie, bearer)
        except ValueError as e:
            ctx = common_context(request)
            ctx["error"] = str(e)
            return templates.TemplateResponse(request, "auth.html", ctx, status_code=400)

        ctx = common_context(request)
        if status.ok:
            ctx["message"] = f"顽鹿登录态已验证：{status.detail}"
        else:
            ctx["error"] = status.detail or "顽鹿验证失败。"
        return templates.TemplateResponse(
            request,
            "auth.html",
            ctx,
            status_code=200 if status.ok else 400,
        )

    @app.post("/sync/start", response_class=HTMLResponse)
    def sync_start(
        request: Request,
        mode: Annotated[str, Form()] = "incremental",
    ):
        if mode == "latest":
            jobs.start_latest()
        else:
            jobs.start_incremental()
        ctx = {"request": request, "job": jobs.snapshot()}
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(request, "partials/sync_status.html", ctx)
        return RedirectResponse("/", status_code=303)

    @app.post("/sync/activity/{activity_id}", response_class=HTMLResponse)
    def sync_activity(request: Request, activity_id: str):
        jobs.start_activity(activity_id)
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "partials/sync_status.html",
                {"request": request, "job": jobs.snapshot()},
            )
        return RedirectResponse("/", status_code=303)

    @app.get("/sync/status", response_class=HTMLResponse)
    def sync_status_page(request: Request):
        return templates.TemplateResponse(
            request,
            "partials/sync_status.html",
            {"request": request, "job": jobs.snapshot()},
        )

    @app.get("/history", response_class=HTMLResponse)
    def history_page(request: Request):
        ctx = common_context(request)
        ctx["rows"] = recent_sync_rows(limit=50)
        return templates.TemplateResponse(request, "history.html", ctx)

    return app


app = create_app()

