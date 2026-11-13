import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from usage.agents import AGENTS, AgentUnavailable
from usage.agents.claude import Claude
from usage.agents.codex import Codex
from usage.demo import DemoLimits
from usage.history import Usage
from usage.limits import Limits
from usage.scheduler import (
    BACKOFF_SECONDS,
    DEFAULT_SCHEDULE,
    FORECASTERS,
    LOOKAHEAD_DAYS,
    SLOT_MINUTES,
    Scheduler,
    UsageModel,
    availability,
    first_window,
    stored_schedule,
    validate_schedule,
)

SETTINGS = dict(refresh_seconds=60, timeout_seconds=2, stale_seconds=300)
# Thursday; the scheduler learns from complete days before it.
TODAY = date(2026, 11, 12)
# Weekly resets stay in the future whenever the tests run.
FAR = 4102444800


def local_ts(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute).astimezone().timestamp()


def history(blocks, days=63, agent="claude", seed=1):
    """Prompts every 10-25 minutes inside blocks(day, age) -> [(start hour, end hour)]."""
    rng = random.Random(seed)
    rows = []
    for age in range(days, 0, -1):
        day = TODAY - timedelta(days=age)
        for start, end in blocks(day, age):
            moment = local_ts(day, start) + rng.randint(-15, 15) * 60
            while moment < local_ts(day, end):
                rows.append({"agent": agent, "start": moment, "tokens": rng.randint(5000, 80000)})
                moment += rng.randint(10, 25) * 60
    return rows


def weekdays(*blocks):
    return lambda day, age: list(blocks) if day.weekday() < 5 else []


def slot(hour, minute=0):
    return (hour * 60 + minute) // SLOT_MINUTES


class ClockLimits:
    """Claude's limits on the test's clock: each activation opens a 5-hour window."""

    def __init__(self, *windows):
        self.windows = [dict(bucket=None, minutes=10080, used_percent=20, resets_at=FAR)]
        self.windows += windows
        self.started = []

    def snapshot(self, connections=None):
        return {"agents": [{"id": "claude", "windows": list(self.windows)}]}

    def activate(self, agent, now=None):
        if any(w["minutes"] < 10080 and w["resets_at"] > now for w in self.windows):
            return dict(activated=False, reason="already_active", windows=self.windows)
        self.started.append(now)
        opened = dict(bucket=None, minutes=300, used_percent=1, resets_at=now + 18000)
        self.windows = [*(w for w in self.windows if w["minutes"] >= 10080), opened]
        return dict(activated=True, reason="activated", windows=self.windows)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = dict(
            history=dict(database=str(self.root / "history.sqlite3")),
            agents={agent: dict(history=str(self.root / agent)) for agent in AGENTS},
            collection=dict(refresh_seconds=1),
            limits=SETTINGS,
            scheduler=dict(refresh_seconds=15),
            prices={},
        )
        self.usage = Usage(self.config)
        self.addCleanup(self.usage.close)
        self.limits = Limits(self.config)

    def scheduler(self, rows, limits=None):
        self.usage.snapshot = Mock(return_value={"rows": rows, "warnings": []})
        return Scheduler(self.usage, limits or self.limits, self.config)

    def test_settings_validation_legacy_format_and_persistence(self):
        self.assertEqual(validate_schedule({}), DEFAULT_SCHEDULE)
        self.assertEqual(
            validate_schedule({"enabled": True, "times": ["13:30", "07:00", "07:00"]}),
            {"enabled": True, "times": ["07:00", "13:30"]},
        )
        for invalid in (
            None,
            [],
            {"enabled": 1},
            {"enabled": "yes"},
            {"times": []},
            {"times": "07:00"},
            {"times": ["7:00"]},
            {"times": ["24:00"]},
            {"times": [f"{h:02d}:00" for h in range(9)]},
            {"mode": "smart"},
            {"extra": 1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_schedule(invalid)
        # Settings saved by the earlier mode-based scheduler keep working.
        for legacy, expected in (
            ({"mode": "manual", "days": [0], "times": ["07:00"]}, DEFAULT_SCHEDULE),
            ({"mode": "smart", "max_weekly_percent": 90}, {"enabled": True, "times": None}),
            ({"mode": "auto"}, {"enabled": True, "times": None}),
            ({"mode": "scheduled", "times": ["09:30"]}, {"enabled": True, "times": ["09:30"]}),
        ):
            self.assertEqual(stored_schedule(legacy), expected)

        scheduler = self.scheduler([])
        self.assertEqual(scheduler.get_schedule("claude"), DEFAULT_SCHEDULE)
        scheduler.set_schedule("claude", {"enabled": True})
        scheduler.set_schedule("codex", {"enabled": False, "times": ["15:00", "09:30"]})
        restarted = Scheduler(self.usage, self.limits, self.config)
        self.assertEqual(restarted.get_schedule("claude"), {"enabled": True, "times": None})
        self.assertEqual(
            restarted.get_schedule("codex"), {"enabled": False, "times": ["09:30", "15:00"]}
        )
        self.usage.store("schedules", {"claude": {"mode": "scheduled", "times": ["08:00"]}})
        self.assertEqual(
            Scheduler(self.usage, self.limits, self.config).get_schedule("claude"),
            {"enabled": True, "times": ["08:00"]},
        )
        with self.assertRaises(ValueError):
            restarted.set_schedule("unknown", {"enabled": True})

    def test_steady_workdays_get_an_early_start_so_resets_land_during_work(self):
        now = local_ts(TODAY, 1)
        model = UsageModel(history(weekdays((9, 17))), now, 300)
        self.assertTrue(model.ready)
        activity, busy, _ = model.forecast(TODAY)
        self.assertGreater(activity[slot(11)], 0.8)
        self.assertLess(activity[slot(3)], 0.1)
        self.assertGreater(busy, 0.9)

        starts, gain = model.plan(TODAY)
        self.assertEqual(len(starts), 1)
        self.assertGreater(gain, 0)
        # Without a start the day has two windows (09:00 and 14:00); the early start makes the
        # quota reset twice while working, so the day has three.
        first_reset, second_reset = starts[0] + 300, starts[0] + 600
        self.assertLess(starts[0], 9 * 60)
        self.assertTrue(9 * 60 < first_reset < second_reset < 17 * 60)

        # A day without expected use still gets a start, though it gains nothing.
        saturday = TODAY + timedelta(days=2)
        self.assertLess(model.forecast(saturday)[1], 0.2)
        starts, gain = model.plan(saturday)
        self.assertEqual(len(starts), 1)
        self.assertLess(gain, 0)

    def test_a_changed_routine_takes_over_within_days_but_one_odd_day_does_not(self):
        now = local_ts(TODAY, 1)

        def switched(after):
            def blocks(day, age):
                if day.weekday() >= 5:
                    return []
                newer = sum(
                    1 for back in range(1, age + 1) if (TODAY - timedelta(days=back)).weekday() < 5
                )
                return [(13, 18)] if newer <= after else [(8, 12)]

            return history(blocks, days=90)

        odd = UsageModel(switched(1), now, 300)
        trust = dict(zip(FORECASTERS, odd.trust))
        self.assertLess(trust["recent"], 0.5)
        activity = odd.forecast(TODAY)[0]
        self.assertGreater(activity[slot(9)], activity[slot(15)])

        moved = UsageModel(switched(3), now, 300)
        trust = dict(zip(FORECASTERS, moved.trust))
        self.assertEqual(max(trust, key=trust.get), "recent")
        activity = moved.forecast(TODAY)[0]
        self.assertGreater(activity[slot(15)], activity[slot(9)])

        settled = UsageModel(switched(8), now, 300)
        starts, _ = settled.plan(TODAY)
        self.assertEqual(len(starts), 1)
        # The new routine gets a reset inside it.
        resets = [starts[0] + k * 300 for k in (1, 2, 3)]
        self.assertTrue(any(13 * 60 < reset < 18 * 60 for reset in resets))

        # While away the day's start gains little; a couple of days back restore the usual plan.
        away = UsageModel(history(lambda d, age: [(9, 17)] if age > 10 else [], 90), now, 300)
        starts, gain = away.plan(TODAY)
        self.assertEqual(len(starts), 1)
        self.assertLess(gain, 0.05)
        back = UsageModel(
            history(lambda d, age: [(9, 17)] if d.weekday() < 5 and not 2 < age <= 12 else [], 90),
            now,
            300,
        )
        starts, gain = back.plan(TODAY)
        self.assertEqual(len(starts), 1)
        self.assertGreater(gain, 0.5)

    def test_a_short_session_gets_a_reset_inside_it_at_a_time_the_app_usually_runs(self):
        now = local_ts(TODAY, 1)
        # An hour a day uses a fraction of any quota, yet a reset inside it doubles the quota
        # available while working: the most usage the limit allows.
        starts, gain = UsageModel(history(weekdays((10, 11))), now, 300).plan(TODAY)
        self.assertEqual(len(starts), 1)
        self.assertGreater(gain, 0)
        self.assertTrue(10 * 60 - 30 <= starts[0] + 300 < 11 * 60)

        self.assertEqual(availability({}, TODAY), [1.0] * 48)
        recent = {(TODAY - timedelta(days=1)).isoformat(): 1}
        self.assertEqual(availability(recent, TODAY), [1.0] * 48)
        daytime = sum(1 << s for s in range(slot(8), slot(20)))
        online = {(TODAY - timedelta(days=age)).isoformat(): daytime for age in range(1, 15)}
        shares = availability(online, TODAY)
        self.assertLess(shares[slot(5)], 0.5)
        self.assertGreater(shares[slot(10)], 0.9)
        model = UsageModel(history(weekdays((9, 17))), now, 300, online=online)
        self.assertTrue(all(start >= 8 * 60 for start in model.plan(TODAY)[0]))
        # When no time is usual for the app, any time may start the day.
        scattered = {
            (TODAY - timedelta(days=age)).isoformat(): 1 << (age * 7 % 48) for age in range(1, 15)
        }
        model = UsageModel(history(weekdays((9, 17))), now, 300, online=scattered)
        self.assertFalse(any(model.allowed))
        self.assertEqual(len(model.plan(TODAY)[0]), 1)

    def test_a_start_is_planned_every_day_even_when_none_is_expected_to_help(self):
        # One prompt a day fits a single window wherever windows start: no start gains anything.
        rows = [
            {"agent": "claude", "start": local_ts(day, 10, 5), "tokens": 1000}
            for day in (TODAY - timedelta(days=age) for age in range(1, 64))
            if day.weekday() < 5
        ]
        now = local_ts(TODAY, 1)
        model = UsageModel(rows, now, 300)
        for offset in range(LOOKAHEAD_DAYS):
            starts, gain = model.plan(TODAY + timedelta(days=offset))
            self.assertEqual(len(starts), 1)
            self.assertLess(gain, 0)

        scheduler = self.scheduler(rows, ClockLimits())
        scheduler.set_schedule("claude", {"enabled": True})
        snap = scheduler.snapshot(now=now)["agents"]["claude"]
        self.assertEqual(snap["status"], "scheduled")
        plan = snap["plan"]
        self.assertEqual(
            (plan["date"], plan["source"], len(plan["starts"])), ("2026-11-12", "learned", 1)
        )
        start = local_ts(TODAY, *map(int, plan["starts"][0].split(":")))
        self.assertEqual(snap["next_activation_at"], start)

    def test_planned_starts_run_once_only_when_enabled_and_custom_times_apply_every_day(self):
        now = local_ts(TODAY, 3)
        idle = [dict(bucket=None, minutes=10080, used_percent=20, resets_at=FAR)]

        def started(at):
            return dict(
                activated=True,
                reason="activated",
                windows=[
                    dict(bucket=None, minutes=300, used_percent=1, resets_at=at + 18000),
                    *idle,
                ],
            )

        with (
            patch.object(Claude, "read_limits", new_callable=AsyncMock, return_value=idle),
            patch.object(Codex, "read_limits", new_callable=AsyncMock, return_value=[]),
            patch.object(Claude, "activate_window", new_callable=AsyncMock) as activate,
            patch.object(Claude, "command", return_value=["claude"]),
            patch.object(Codex, "command", return_value=["codex"]),
        ):
            scheduler = self.scheduler(history(weekdays((9, 17))))
            snap = scheduler.snapshot(now=now)["agents"]["claude"]
            self.assertEqual(snap["status"], "off")
            self.assertIsNone(snap["next_activation_at"])
            plan = snap["plan"]
            self.assertEqual(
                (plan["date"], plan["today"], plan["source"]), ("2026-11-12", True, "learned")
            )
            self.assertEqual(len(plan["activity"]), 48)
            self.assertEqual(len(plan["starts"]), 1)
            start = local_ts(TODAY, *map(int, plan["starts"][0].split(":")))
            self.assertEqual(scheduler.tick(now=start + 60), [])
            activate.assert_not_awaited()

            scheduler.set_schedule("claude", {"enabled": True})
            snap = scheduler.snapshot(now=now)["agents"]["claude"]
            self.assertEqual(snap["status"], "scheduled")
            self.assertEqual(snap["next_activation_at"], start)
            self.assertEqual(snap["model"]["active_days"], 45)
            self.assertEqual(scheduler.tick(now=start - 60), [])

            activate.return_value = started(start + 60)
            events = scheduler.tick(now=start + 60)
            self.assertEqual([e["status"] for e in events], ["activated"])
            self.assertEqual(events[0]["mode"], "learned")
            self.assertEqual(scheduler.tick(now=start + 180), [])
            self.assertEqual(activate.await_count, 1)
            following = scheduler.snapshot(now=start + 180)["agents"]["claude"]
            reset = start + 60 + 18000
            self.assertEqual(following["active_until"], reset)
            # The day's chain goes on: the next window starts right when this one resets.
            self.assertEqual(following["plan"]["date"], "2026-11-12")
            self.assertEqual(following["next_activation_at"], reset)
            self.assertEqual(scheduler.wake_at, reset)

            # Fixed times apply every day, used or not: Friday 14:00, then Saturday 14:00.
            scheduler.set_schedule("claude", {"enabled": True, "times": ["14:00"]})
            two = local_ts(TODAY, 14)
            snap = scheduler.snapshot(now=two - 600)["agents"]["claude"]
            self.assertEqual(
                (snap["plan"]["source"], snap["plan"]["starts"]), ("custom", ["14:00"])
            )
            activate.return_value = started(two + 60)
            self.assertEqual(scheduler.tick(now=two + 60)[0]["mode"], "custom")
            self.assertEqual(activate.await_count, 2)
            # Missed on Friday while the app was closed, it is due as soon as the app runs.
            friday_evening = local_ts(TODAY + timedelta(days=1), 20)
            self.assertEqual(
                scheduler.snapshot(now=friday_evening)["agents"]["claude"]["next_activation_at"],
                friday_evening,
            )
            self.assertEqual(
                scheduler.snapshot(now=local_ts(TODAY + timedelta(days=2), 10))["agents"]["claude"][
                    "next_activation_at"
                ],
                local_ts(TODAY + timedelta(days=2), 14),
            )

            # The app records when it is running, for later plans.
            self.assertTrue(scheduler.online["2026-11-12"])
            restarted = Scheduler(self.usage, self.limits, self.config)
            self.assertEqual(restarted.online, scheduler.online)
            self.assertEqual(len(restarted.history), 2)

    def test_a_late_start_runs_once_the_app_runs_and_the_chain_follows_it(self):
        limits = ClockLimits()
        scheduler = self.scheduler([], limits)
        scheduler.set_schedule("claude", {"enabled": True, "times": ["09:30", "14:30"]})
        # On three days the computer is turned on after the 09:30 start.
        for offset, opened in enumerate(((9, 46), (12, 0), (23, 59))):
            day = TODAY + timedelta(days=offset)
            now, midnight = local_ts(day, *opened), local_ts(day + timedelta(days=1), 0)
            with self.subTest(opened=opened):
                snap = scheduler.snapshot(now=now)["agents"]["claude"]
                # Only the earliest time starts the day; the chain covers the rest of it.
                self.assertEqual(snap["plan"]["starts"], ["09:30"])
                self.assertEqual((snap["status"], snap["next_activation_at"]), ("scheduled", now))
                events = scheduler.tick(now=now)
                self.assertEqual(
                    [(e["status"], e["mode"]) for e in events], [("activated", "custom")]
                )
                self.assertEqual(limits.started[-1], now)
                # The next window follows the late one's reset, not the planned 14:30, and
                # nothing carries past midnight.
                reset = now + 18000
                expected = reset if reset < midnight else local_ts(day + timedelta(days=1), 9, 30)
                self.assertEqual(scheduler.wake_at, expected)
                following = scheduler.snapshot(now=now + 60)["agents"]["claude"]
                self.assertEqual(following["next_activation_at"], expected)
                # The chart shows the day of the next start: the scheduler's open window and
                # the missed start, or the next day when the window crosses midnight.
                plan, same_day = following["plan"], reset < midnight
                at = opened[0] * 60 + opened[1] - (0 if same_day else 1440)
                self.assertEqual(
                    plan["windows"],
                    [{"start": at, "end": at + 300, "by": "scheduler", "open": True}],
                )
                self.assertEqual(plan["start_status"], "missed" if same_day else "pending")
        self.assertEqual(len(limits.started), 3)

    def test_a_window_opened_before_the_start_begins_the_chain_until_midnight(self):
        # The user's own prompt at 06:00 opened a window that is still open at 09:30.
        own = dict(bucket=None, minutes=300, used_percent=20, resets_at=local_ts(TODAY, 11))
        limits = ClockLimits(own)
        prompts = [
            {"agent": "claude", "start": local_ts(TODAY, hour, minute), "tokens": 1000}
            for hour, minute in ((6, 0), (10, 30), (12, 0), (15, 45))
        ]
        scheduler = self.scheduler(prompts, limits)
        scheduler.set_schedule("claude", {"enabled": True, "times": ["09:30"]})

        events = scheduler.tick(now=local_ts(TODAY, 9, 30))
        self.assertEqual([e["status"] for e in events], ["skipped_active"])
        eleven = local_ts(TODAY, 11)
        self.assertEqual(scheduler.wake_at, eleven)
        snap = scheduler.snapshot(now=local_ts(TODAY, 9, 31))["agents"]["claude"]
        self.assertEqual(snap["next_activation_at"], eleven)
        self.assertEqual(scheduler.tick(now=eleven - 1), [])

        # Each reset is followed at once. The prompts at 12:00 and 15:45 belong to the window
        # that resets at 16:00, so they do not hold the next one back.
        tomorrow = TODAY + timedelta(days=1)
        for hour, wake in ((11, local_ts(TODAY, 16)), (16, local_ts(TODAY, 21)), (21, None)):
            moment = local_ts(TODAY, hour)
            self.assertEqual([e["status"] for e in scheduler.tick(now=moment)], ["activated"])
            self.assertEqual(scheduler.wake_at, wake or local_ts(tomorrow, 9, 30))
            if hour == 11:
                # The chart gets the real day: the user's window, the scheduler's, and why.
                plan = scheduler.snapshot(now=local_ts(TODAY, 12))["agents"]["claude"]["plan"]
                self.assertEqual(
                    plan["windows"],
                    [
                        {"start": 360, "end": 660, "by": "you", "open": False},
                        {"start": 660, "end": 960, "by": "scheduler", "open": True},
                    ],
                )
                self.assertEqual(
                    plan["events"],
                    [{"at": 570, "status": "skipped_active"}, {"at": 660, "status": "activated"}],
                )
                # The 06:00 window began the day's chain before its 09:30 start.
                self.assertEqual((plan["start_status"], plan["next"]), ("early", 960))
        self.assertEqual(limits.started, [local_ts(TODAY, hour) for hour in (11, 16, 21)])

        # The 21:00 window resets at 02:00 and shows on the next day's chart up to then.
        plan = scheduler.snapshot(now=local_ts(TODAY, 21, 30))["agents"]["claude"]["plan"]
        self.assertEqual(plan["date"], "2026-11-13")
        self.assertEqual(
            plan["windows"], [{"start": -180, "end": 120, "by": "scheduler", "open": True}]
        )
        self.assertEqual((plan["start_status"], plan["next"], plan["events"]), ("pending", 570, []))

        # The next day's chain begins at its own start, on time.
        self.assertEqual(scheduler.tick(now=local_ts(tomorrow, 2)), [])
        self.assertEqual(
            [e["status"] for e in scheduler.tick(now=local_ts(tomorrow, 9, 30))], ["activated"]
        )
        plan = scheduler.snapshot(now=local_ts(tomorrow, 9, 31))["agents"]["claude"]["plan"]
        self.assertEqual((plan["start_status"], plan["next"]), ("ran", 870))

    def test_a_window_from_another_device_is_renewed_from_its_reset_until_midnight(self):
        limits = ClockLimits()
        scheduler = self.scheduler([], limits)
        scheduler.set_schedule("claude", {"enabled": True, "times": ["20:00"]})
        tomorrow = TODAY + timedelta(days=1)
        self.assertEqual(scheduler.tick(now=local_ts(TODAY, 12)), [])
        self.assertEqual(scheduler.wake_at, local_ts(TODAY, 20))

        # At 12:30 a window opens elsewhere: the agent reports it, no local prompt does.
        # The scheduler notices it on its own, with nothing showing the widget.
        reset = local_ts(TODAY, 17, 30)
        limits.windows.append(dict(bucket=None, minutes=300, used_percent=20, resets_at=reset))
        events = scheduler.tick(now=local_ts(TODAY, 12, 31))
        self.assertEqual([e["status"] for e in events], ["skipped_active"])
        self.assertEqual(scheduler.wake_at, reset)
        snap = scheduler.snapshot(now=local_ts(TODAY, 12, 32))["agents"]["claude"]
        self.assertEqual((snap["status"], snap["next_activation_at"]), ("scheduled", reset))
        plan = snap["plan"]
        self.assertEqual(plan["starts"], ["20:00"])
        self.assertEqual((plan["start_status"], plan["next"]), ("early", 1050))
        self.assertEqual(plan["windows"], [{"start": 750, "end": 1050, "by": "you", "open": True}])

        # From its reset, windows are renewed until midnight; the last one crosses it.
        for hour, minute, wake in ((17, 30, local_ts(TODAY, 22, 30)), (22, 30, None)):
            events = scheduler.tick(now=local_ts(TODAY, hour, minute))
            self.assertEqual([e["status"] for e in events], ["activated"])
            self.assertEqual(scheduler.wake_at, wake or local_ts(tomorrow, 20))
        self.assertEqual(limits.started, [reset, local_ts(TODAY, 22, 30)])

        # A window from the day before never begins the next day's chain.
        self.assertEqual(scheduler.tick(now=local_ts(tomorrow, 3, 30)), [])
        self.assertEqual(scheduler.wake_at, local_ts(tomorrow, 20))
        plan = scheduler.snapshot(now=local_ts(tomorrow, 3, 31))["agents"]["claude"]["plan"]
        self.assertEqual((plan["start_status"], plan["next"]), ("pending", 1200))

    def test_a_window_that_ended_while_the_app_was_closed_still_begins_the_chain(self):
        def prompts(*moments):
            return [{"agent": "claude", "start": local_ts(*m), "tokens": 1000} for m in moments]

        yesterday = TODAY - timedelta(days=1)
        seconds = 300 * 60
        nine = local_ts(TODAY, 9)
        # Prompts after a reset open a window; ones inside a window from the day before do not.
        starts = [r["start"] for r in prompts((yesterday, 22), (TODAY, 0, 30), (TODAY, 3, 30))]
        self.assertEqual(first_window([], starts[:2], TODAY, nine, seconds), None)
        self.assertEqual(first_window([], starts, TODAY, nine, seconds), starts[2])
        known = [{"start": local_ts(yesterday, 21), "end": local_ts(TODAY, 2)}]
        self.assertEqual(first_window(known, starts[1:2], TODAY, nine, seconds), None)
        known.append({"start": local_ts(TODAY, 6), "end": local_ts(TODAY, 11)})
        self.assertEqual(first_window(known, starts, TODAY, nine, seconds), starts[2])
        # Nothing counts before it happens.
        self.assertEqual(first_window(known, starts, TODAY, local_ts(TODAY, 3), seconds), None)

        # The user worked from 01:00 to 01:20 with the app closed; it opens at 09:00.
        limits = ClockLimits()
        scheduler = self.scheduler(prompts((TODAY, 1), (TODAY, 1, 20)), limits)
        scheduler.set_schedule("claude", {"enabled": True, "times": ["20:00"]})
        self.assertEqual([e["status"] for e in scheduler.tick(now=nine)], ["activated"])
        self.assertEqual(scheduler.wake_at, local_ts(TODAY, 14))

    def test_a_window_from_the_day_before_defers_the_start(self):
        own = dict(bucket=None, minutes=300, used_percent=20, resets_at=local_ts(TODAY, 3))
        limits = ClockLimits(own)
        scheduler = self.scheduler([], limits)
        scheduler.set_schedule("claude", {"enabled": True, "times": ["01:00"]})
        events = scheduler.tick(now=local_ts(TODAY, 1))
        self.assertEqual([e["status"] for e in events], ["skipped_active"])
        plan = scheduler.snapshot(now=local_ts(TODAY, 1, 1))["agents"]["claude"]["plan"]
        self.assertEqual((plan["start_status"], plan["next"]), ("deferred", 180))
        self.assertEqual(scheduler.tick(now=local_ts(TODAY, 3))[0]["status"], "activated")

    def test_a_start_missed_yesterday_does_not_run_the_next_day(self):
        limits = ClockLimits()
        scheduler = self.scheduler([], limits)
        scheduler.set_schedule("claude", {"enabled": True, "times": ["23:50"]})
        # The computer was off at yesterday's 23:50 and is turned on after midnight.
        after_midnight, tonight = local_ts(TODAY, 0, 5), local_ts(TODAY, 23, 50)
        self.assertEqual(scheduler.tick(now=after_midnight), [])
        self.assertEqual(scheduler.wake_at, tonight)
        snap = scheduler.snapshot(now=after_midnight)["agents"]["claude"]
        self.assertEqual(
            (snap["next_activation_at"], snap["plan"]["date"]), (tonight, "2026-11-12")
        )
        self.assertEqual(scheduler.tick(now=tonight)[0]["status"], "activated")
        self.assertEqual(limits.started, [tonight])

    def test_limits_and_connection_pause_starts(self):
        now = local_ts(TODAY, 5)
        spent = [
            dict(bucket=None, minutes=300, used_percent=0, resets_at=now + 18000),
            dict(bucket=None, minutes=10080, used_percent=100, resets_at=FAR),
        ]
        weekly = [dict(bucket=None, minutes=10080, used_percent=5, resets_at=FAR)]
        with (
            patch.object(Claude, "read_limits", new_callable=AsyncMock, return_value=spent),
            patch.object(Codex, "read_limits", new_callable=AsyncMock, return_value=weekly),
            patch.object(Claude, "activate_window", new_callable=AsyncMock) as activate,
            patch.object(Claude, "command", return_value=["claude"]),
            patch.object(Codex, "command", return_value=["codex"]),
        ):
            scheduler = self.scheduler([])
            scheduler.set_schedule("claude", {"enabled": True, "times": ["05:00"]})
            snap = scheduler.snapshot(now=now)["agents"]["claude"]
            self.assertEqual((snap["status"], snap["reason"]), ("paused", "weekly_limit"))
            events = scheduler.tick(now=now + 60)
            self.assertEqual([e["status"] for e in events], ["skipped_exhausted"])
            self.assertEqual(scheduler.tick(now=now + 120), [])
            activate.assert_not_awaited()

            self.usage.set_connection("claude", False)
            scheduler.set_schedule("claude", {"enabled": True, "times": ["06:00"]})
            self.assertEqual(scheduler.tick(now=local_ts(TODAY, 6, 1)), [])
            snap = scheduler.snapshot(now=local_ts(TODAY, 6, 1))["agents"]["claude"]
            self.assertEqual((snap["status"], snap["reason"]), ("paused", "disconnected"))
            activate.assert_not_awaited()

            # Without history the learned plan waits for data instead of guessing.
            scheduler.set_schedule("codex", {"enabled": True})
            self.assertEqual(scheduler.snapshot(now=now)["agents"]["codex"]["status"], "learning")

    def test_failures_retry_after_each_backoff_and_hide_private_details(self):
        now = local_ts(TODAY, 10)
        windows = [dict(bucket=None, minutes=10080, used_percent=10, resets_at=FAR)]
        with (
            patch.object(Claude, "read_limits", new_callable=AsyncMock, return_value=windows),
            patch.object(Codex, "read_limits", new_callable=AsyncMock, return_value=[]),
            patch.object(
                Claude,
                "activate_window",
                new_callable=AsyncMock,
                side_effect=RuntimeError("PRIVATE_SECRET_KEY"),
            ) as activate,
            patch.object(Claude, "command", return_value=["claude"]),
            patch.object(Codex, "command", return_value=["codex"]),
        ):
            scheduler = self.scheduler([])
            scheduler.set_schedule("claude", {"enabled": True, "times": ["10:00"]})
            events = scheduler.tick(now=now + 30)
            self.assertEqual((events[0]["status"], events[0]["reason"]), ("failed", "query_failed"))
            self.assertNotIn("PRIVATE_SECRET_KEY", json.dumps(scheduler.snapshot(now=now + 30)))
            self.assertEqual(scheduler.tick(now=now + 60), [])
            self.assertEqual(activate.await_count, 1)
            self.assertEqual(scheduler.wake_at, now + 30 + BACKOFF_SECONDS)
            # A start that keeps failing is retried after every backoff, long past its time.
            retry = now + 30 + BACKOFF_SECONDS
            self.assertEqual(scheduler.tick(now=retry)[0]["status"], "failed")
            activate.side_effect = None
            activate.return_value = dict(activated=True, reason="activated", windows=windows)
            late = retry + 3 * BACKOFF_SECONDS
            self.assertEqual(scheduler.tick(now=late)[0]["status"], "activated")
            self.assertEqual(activate.await_count, 3)

            for error, expected in (
                (FileNotFoundError("/secret/path"), "not_found"),
                (TimeoutError("secret"), "timeout"),
                (AgentUnavailable("login_required"), "login_required"),
            ):
                activate.side_effect = error
                result = scheduler.activate("claude", now=now + 20000)
                self.assertEqual(result["reason"], expected)
                self.assertNotIn("secret", json.dumps(result))

    def test_capacity_samples_from_earlier_versions_are_dropped(self):
        saved = {"claude": {"capacity": {"2990000": 20.0}, "active_until": 5.0}}
        self.usage.store("scheduler_state", {"state": saved})
        scheduler = Scheduler(self.usage, self.limits, self.config)
        self.assertEqual(scheduler.state["claude"], {"active_until": 5.0})

    def test_demo_activation_uses_no_agent_process(self):
        now = local_ts(TODAY, 10)
        with patch.object(Codex, "activate_window", new_callable=AsyncMock) as live:
            scheduler = self.scheduler(history(weekdays((9, 17)), agent="codex"), DemoLimits())
            self.assertEqual(scheduler.activate("codex", now=now)["status"], "activated")
            self.assertEqual(scheduler.activate("codex", now=now + 60)["status"], "skipped_active")
            live.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
