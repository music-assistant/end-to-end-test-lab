"""Tone run merging of the queue checks."""

from labctl.analyze import Segment
from scenarios.checks.queue import starts_after, tone_runs

KINDS = ("a2t1", "a2t2", "a2t3")


def test_merges_runs_across_short_interruptions() -> None:
    """Same-kind segments separated by less than 1 s of silence, noise or overlap become one."""
    timeline = [
        Segment("a2t1", 0.0, 10.0, -20.0),
        Segment("unknown", 10.0, 10.3, -40.0),
        Segment("a2t1", 10.3, 25.0, -20.0),
        Segment("mixed", 25.0, 25.5, -25.0),
        Segment("a2t2", 25.5, 40.0, -20.0),
        Segment("silence", 40.0, 40.8, -90.0),
        Segment("a2t2", 40.8, 50.0, -20.0),
    ]
    assert tone_runs(timeline, KINDS) == [
        Segment("a2t1", 0.0, 25.0, -20.0),
        Segment("a2t2", 25.5, 50.0, -20.0),
    ]


def test_long_interruption_splits_and_order_is_kept() -> None:
    """A gap of 1 s or more splits a tone, other kinds are dropped and order is kept."""
    timeline = [
        Segment("a2t2", 0.0, 5.0, -20.0),
        Segment("silence", 5.0, 7.0, -90.0),
        Segment("a2t2", 7.0, 9.0, -20.0),
        Segment("a1t1", 9.0, 12.0, -20.0),
        Segment("a2t1", 12.0, 20.0, -20.0),
    ]
    assert [(run.kind, run.t0, run.t1) for run in tone_runs(timeline, KINDS)] == [
        ("a2t2", 0.0, 5.0),
        ("a2t2", 7.0, 9.0),
        ("a2t1", 12.0, 20.0),
    ]


def test_starts_after_allows_one_window() -> None:
    """A tone stamped up to one window before the command counts as after it."""
    assert starts_after(Segment("a2t1", 9.95, 12.0, -20.0), 10.0, 0.085)
    assert not starts_after(Segment("a2t1", 9.8, 12.0, -20.0), 10.0, 0.085)
    assert starts_after(Segment("a2t1", 10.0, 12.0, -20.0), 10.0, 0.085)
