"""Single-task sync runner for the localhost web UI."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from stravalib.client import Client as StravaClient

from ..onelap import Activity, OnelapClient
from ..onelap.auth import get_authenticated_onelap_client
from ..strava_auth import get_authenticated_client
from ..sync import DEFAULT_CACHE_DIR, SyncReport, run_sync


@dataclass
class SyncJobSnapshot:
    running: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    report: SyncReport | None = None
    error: str | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def has_result(self) -> bool:
        return self.report is not None or self.error is not None


class SyncJobManager:
    """Run at most one sync job in a background thread."""

    def __init__(
        self,
        *,
        runner: Callable[..., SyncReport] = run_sync,
        strava_factory: Callable[[], StravaClient] | None = None,
        onelap_factory: Callable[[], OnelapClient] | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
    ) -> None:
        self._runner = runner
        self._strava_factory = strava_factory or (
            lambda: get_authenticated_client(interactive=False)
        )
        self._onelap_factory = onelap_factory or get_authenticated_onelap_client
        self._cache_dir = cache_dir
        self._lock = threading.Lock()
        self._snapshot = SyncJobSnapshot()

    def snapshot(self) -> SyncJobSnapshot:
        with self._lock:
            return SyncJobSnapshot(
                running=self._snapshot.running,
                started_at=self._snapshot.started_at,
                finished_at=self._snapshot.finished_at,
                report=self._snapshot.report,
                error=self._snapshot.error,
                messages=list(self._snapshot.messages),
            )

    def start_incremental(self) -> bool:
        return self._start(
            message="正在批量拉取顽鹿新骑行、修正坐标并上传到 Strava...",
            incremental=True,
            limit=1,
            activity_id=None,
        )

    def start_latest(self) -> bool:
        return self._start(
            message="正在同步顽鹿最新一条骑行...",
            incremental=False,
            limit=1,
            activity_id=None,
        )

    def start_activity(self, activity_id: str) -> bool:
        return self._start(
            message=f"正在同步顽鹿活动 {activity_id}...",
            incremental=False,
            limit=1,
            activity_id=activity_id,
        )

    def _start(
        self,
        *,
        message: str,
        incremental: bool,
        limit: int,
        activity_id: str | None,
    ) -> bool:
        with self._lock:
            if self._snapshot.running:
                return False
            self._snapshot = SyncJobSnapshot(
                running=True,
                started_at=datetime.now(tz=timezone.utc),
                messages=[message],
            )

        thread = threading.Thread(
            target=self._run,
            kwargs={
                "incremental": incremental,
                "limit": limit,
                "activity_id": activity_id,
            },
            daemon=True,
        )
        thread.start()
        return True

    def _run(
        self,
        *,
        incremental: bool,
        limit: int,
        activity_id: str | None,
    ) -> None:
        try:
            onelap = self._onelap_factory()
            if activity_id is not None:
                onelap = _SingleActivityOnelapClient(onelap, activity_id)
            report = self._runner(
                limit=limit,
                incremental=incremental,
                force=False,
                name=None,
                cache_dir=self._cache_dir,
                onelap=onelap,
                strava=self._strava_factory(),
            )
            messages = [r.pretty() for r in report.results]
            if not messages:
                messages = ["没有发现需要同步的骑行。"]
            error = None
        except Exception as e:  # noqa: BLE001 - surfaced to the web page
            report = None
            messages = []
            error = str(e)

        with self._lock:
            self._snapshot.running = False
            self._snapshot.finished_at = datetime.now(tz=timezone.utc)
            self._snapshot.report = report
            self._snapshot.error = error
            self._snapshot.messages = messages


class _SingleActivityOnelapClient:
    """Delegate downloads to a real client, but expose one selected activity."""

    def __init__(self, delegate: OnelapClient, activity_id: str) -> None:
        self._delegate = delegate
        self._activity_id = activity_id

    def list_activities(self, limit: int | None = None) -> list[Activity]:
        for activity in self._delegate.list_activities():
            if activity.activity_id == self._activity_id:
                return [activity]
        raise ValueError(f"Onelap activity not found: {self._activity_id}")

    def download_fit(self, activity: Activity, *, cache_dir: Path):
        return self._delegate.download_fit(activity, cache_dir=cache_dir)

