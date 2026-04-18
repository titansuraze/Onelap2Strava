"""Strava upload + duplicate detection.

The caller supplies a path to an already-corrected Fit and the activity's
start time (which we get from ``fix_fit``'s result). We:

1. Search activities in a +-10 minute window around that start time; if any
   exist, assume the activity is a duplicate and skip.
2. Otherwise upload with a stable ``external_id`` (defaults to a sha1 of the
   file contents so re-uploads of the same exact bytes get deduped by Strava
   as well).
3. Poll until Strava finishes processing and return the new activity's URL.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stravalib.client import Client

logger = logging.getLogger(__name__)

DEDUP_WINDOW = timedelta(minutes=10)
UPLOAD_TIMEOUT_S = 120.0
UPLOAD_POLL_INTERVAL_S = 2.0


@dataclass
class UploadOutcome:
    """What happened during an upload attempt."""

    skipped_duplicate: bool
    existing_activity_id: int | None
    new_activity_id: int | None
    activity_url: str | None
    external_id: str

    def pretty(self) -> str:
        if self.skipped_duplicate:
            return (
                f"[skip] Duplicate detected; existing activity "
                f"https://www.strava.com/activities/{self.existing_activity_id}"
            )
        return f"[ok]   Uploaded: {self.activity_url}"


def _file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_duplicate(
    client: Client, start_time_utc: datetime
) -> tuple[int | None, str | None]:
    """Look for any Strava activity whose start time is within DEDUP_WINDOW.

    Returns ``(activity_id, name)`` of the first match, or ``(None, None)``.
    """
    after = start_time_utc - DEDUP_WINDOW
    before = start_time_utc + DEDUP_WINDOW
    for activity in client.get_activities(after=after, before=before, limit=5):
        return activity.id, activity.name
    return None, None


def upload_fit(
    client: Client,
    fit_path: Path | str,
    start_time_utc: datetime,
    *,
    name: str | None = None,
    external_id: str | None = None,
    activity_type: str | None = None,
    force: bool = False,
) -> UploadOutcome:
    """Upload a (corrected) Fit file to Strava with dedup and polling.

    Parameters
    ----------
    client:
        Authenticated ``stravalib.Client``.
    fit_path:
        Path to the fit file to upload.
    start_time_utc:
        UTC start time of the activity. Used for the dedup query.
    name:
        Optional activity name. Strava will otherwise use defaults.
    external_id:
        Stable identifier. Defaults to ``sha1:<hex>`` of file bytes.
    activity_type:
        Optional Strava activity type string (e.g. ``"Ride"``). Most of the
        time the Fit file already specifies a sport and this can stay None.
    force:
        If True, skip the local time-window dedup check. (Strava will still
        reject truly identical uploads on its side.)
    """
    fit_path = Path(fit_path)
    if not fit_path.exists():
        raise FileNotFoundError(fit_path)
    if start_time_utc.tzinfo is None:
        start_time_utc = start_time_utc.replace(tzinfo=timezone.utc)
    ext_id = external_id or f"sha1:{_file_sha1(fit_path)}"

    if not force:
        existing_id, existing_name = _find_duplicate(client, start_time_utc)
        if existing_id is not None:
            logger.info("Duplicate activity found: %s (%s)", existing_id, existing_name)
            return UploadOutcome(
                skipped_duplicate=True,
                existing_activity_id=existing_id,
                new_activity_id=None,
                activity_url=None,
                external_id=ext_id,
            )

    with fit_path.open("rb") as f:
        uploader = client.upload_activity(
            activity_file=f,
            data_type="fit",
            name=name,
            external_id=ext_id,
            activity_type=activity_type,
        )
        activity = uploader.wait(
            timeout=UPLOAD_TIMEOUT_S, poll_interval=UPLOAD_POLL_INTERVAL_S
        )

    activity_id = int(activity.id)
    url = f"https://www.strava.com/activities/{activity_id}"
    return UploadOutcome(
        skipped_duplicate=False,
        existing_activity_id=None,
        new_activity_id=activity_id,
        activity_url=url,
        external_id=ext_id,
    )
