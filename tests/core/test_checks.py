"""The scenario assertions must actually catch the legacy failure modes."""

from __future__ import annotations

import time

import pytest

from robofast_core.testing.checks import assert_fenced_after_cancel, assert_generations_disjoint
from robofast_core.timeline import Timeline


def _base() -> Timeline:
    tl = Timeline()
    tl.event("seq", "observe_start", gen=1)
    tl.command("seq/observe#1", "camera", "StartExposure", "sent")
    tl.event("seq", "cancel_requested", gen=1)
    tl.event("seq/observe#1", "grant_revoked", device="camera")
    tl.event("seq/observe#1", "grant_revoked", device="telescope")
    tl.command("seq/observe#1", "camera", "ImageReady", "rejected")
    tl.command("seq/cleanup#1", "camera", "AbortExposure", "sent")
    tl.command("seq/cleanup#1", "telescope", "Park", "sent")
    return tl


def test_clean_timeline_passes():
    tl = _base()
    assert_fenced_after_cancel(tl, "seq", 1, ("telescope", "camera"))


def test_stale_task_sending_after_fence_is_caught():
    tl = _base()
    time.sleep(0.001)
    tl.command("seq/observe#1", "telescope", "SlewToCoordinatesAsync", "sent")
    with pytest.raises(AssertionError, match="after it was fenced"):
        assert_fenced_after_cancel(tl, "seq", 1, ("telescope", "camera"))


def test_other_component_commanding_after_cancel_is_caught():
    """C-1/C-2: a leftover guide loop moving the mount after the closure."""
    tl = _base()
    tl.command("guider", "telescope", "PulseGuide", "sent")
    with pytest.raises(AssertionError, match="other components"):
        assert_fenced_after_cancel(tl, "seq", 1, ("telescope", "camera"))


def test_overlapping_generations_are_caught():
    tl = Timeline()
    tl.event("seq", "observe_start", gen=1)
    tl.event("seq", "observe_start", gen=2)
    tl.event("seq", "observe_finished", gen=1)
    tl.event("seq", "cleanup_done", gen=1)
    with pytest.raises(AssertionError, match="not finished"):
        assert_generations_disjoint(tl, "seq")
