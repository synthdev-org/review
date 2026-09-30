"""Unit tests for ``adapter/storage/tenancy.py``'s ``day_for`` — the pure
04:00-anchored-day helper, per
``docs/plans/plan-deliver-admin-gui.md``'s Phase 7 "Tests" section:
"unit tests for ``day_for``'s 04:00 boundary math."

No I/O, no database — ``day_for`` is a pure function of its one argument,
so (unlike every other test in this package) this module needs no
``migrated_engine``/``pgvector_container`` fixture and carries no
``@pytest.mark.integration`` marker.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from adapter.storage.tenancy import day_for


def test_day_for_just_before_reset_belongs_to_the_previous_date() -> None:
    """03:59 — one minute before the 04:00 reset — still belongs to the
    *previous* calendar date's window, per ADR-0008's "reset 04:00, not
    midnight" Decision.
    """
    now = datetime(2026, 3, 15, 3, 59, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 14)


def test_day_for_exactly_at_reset_belongs_to_that_date() -> None:
    """04:00:00 exactly is the first instant of the new day's window."""
    now = datetime(2026, 3, 15, 4, 0, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_just_after_reset_belongs_to_that_date() -> None:
    """04:01 — one minute after the reset — belongs to the new day."""
    now = datetime(2026, 3, 15, 4, 1, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_midday_belongs_to_that_calendar_date() -> None:
    """An ordinary midday timestamp (well clear of either boundary) is the
    straightforward, non-edge-case sanity check.
    """
    now = datetime(2026, 3, 15, 12, 30, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_just_before_midnight_is_unaffected_by_the_date_rollover() -> None:
    """23:59 is nowhere near the 04:00 boundary — this guards against an
    implementation that accidentally anchors on midnight instead of 04:00.
    """
    now = datetime(2026, 3, 15, 23, 59, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 15)


def test_day_for_crosses_a_month_boundary_correctly() -> None:
    """03:59 on the first of a month still belongs to the previous month's
    last date — exercises the boundary math against ``date`` arithmetic
    that has to roll over a month (and, transitively, verifies no
    off-by-one from naive string/int date manipulation).
    """
    now = datetime(2026, 4, 1, 3, 59, 0, tzinfo=UTC)
    assert day_for(now) == date(2026, 3, 31)
