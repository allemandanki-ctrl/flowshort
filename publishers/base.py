"""
Common interface for direct-to-SaaS publishers (Ayrshare, Zernio).

Both providers handle publishing AND server-side scheduling themselves —
FlowShort just uploads the video, gets a hosted URL back, and fires one
scheduled post. No daemon, no polling: fire and forget.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class PublishResult:
    """Outcome of one publish() call. Shaped to match the old Postiz
    ScheduleResult so app.py's logging/summary code needs minimal changes."""
    video: str
    platform: str
    scheduled_time: str
    status: str                    # "created" | "dry-run" | "error"
    detail: str = ""
    post_id: str | None = None


class Publisher(ABC):
    """One instance per provider account (e.g. one ZernioClient per Zernio key)."""

    @abstractmethod
    def publish(
        self,
        video_path: Path,
        caption: str,
        platform: str,
        scheduled_datetime: datetime,
        *,
        title: str = "",
        dry_run: bool = False,
        thumbnail_path: Path | str | None = None,
        **platform_kwargs,
    ) -> PublishResult:
        """Upload the video (if needed), then create one scheduled post."""
        raise NotImplementedError
