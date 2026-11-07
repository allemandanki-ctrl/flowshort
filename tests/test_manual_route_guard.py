"""
Rule n°5 (manual publish queue): the automatic publisher must SKIP any video
whose script route is "manual" -- under no circumstance may one go out through
Zernio/Ayrshare by itself. This is the dedicated unit test for that guard,
in modules.publish.publish_config.schedule_video.

Run:  python -m pytest tests/test_manual_route_guard.py -v
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.publish.publish_config import schedule_video, PublishConfig


def _empty_cfg() -> PublishConfig:
    return PublishConfig(providers={}, platforms={})


def test_manual_route_is_refused_before_any_provider_call():
    """route='manual' must short-circuit BEFORE get_client/provider dispatch is ever reached."""
    with patch("modules.publish.publish_config.get_client") as get_client:
        res = schedule_video(
            "does/not/exist.mp4", ["tiktok"], _fake_time(),
            caption="test", cfg=_empty_cfg(), route="manual",
        )
    get_client.assert_not_called()
    assert res.status == "skipped"
    assert "manual" in res.detail.lower()


def test_auto_route_is_not_blocked_by_the_guard():
    """route=None (the default/'auto' case) must NOT be refused by the manual check --
    it should proceed to the normal file-existence check instead (which is what fails
    here, since the path is fake -- proving the manual guard didn't intercept it)."""
    res = schedule_video(
        "does/not/exist.mp4", ["tiktok"], _fake_time(),
        caption="test", cfg=_empty_cfg(), route=None,
    )
    assert res.status == "error"
    assert "manual" not in res.detail.lower()
    assert "not found" in res.detail.lower()


def test_manual_route_refused_even_with_a_real_video_path(tmp_path):
    """The guard fires on route alone -- a video that DOES exist must still be refused."""
    fake_video = tmp_path / "video.mp4"
    fake_video.write_bytes(b"not a real mp4, doesn't matter for this test")

    with patch("modules.publish.publish_config.get_client") as get_client:
        res = schedule_video(
            fake_video, ["tiktok", "instagram"], _fake_time(),
            caption="test", cfg=_empty_cfg(), route="manual",
        )
    get_client.assert_not_called()
    assert res.status == "skipped"


def _fake_time():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
