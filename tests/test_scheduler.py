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
    SLOT_MINUTES,
    Scheduler,
    UsageModel,
    availability,
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

        saturday = TODAY + timedelta(days=2)
        self.assertLess(model.forecast(saturday)[1], 0.2)
        self.assertEqual(model.plan(saturday), ([], 0.0))

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
        self.assertTrue(starts)
        self.assertTrue(all(start >= 8 * 60 for start in starts))
        self.assertTrue(any(13 * 60 < start + 300 < 18 * 60 for start in starts))

        # While away nothing is started; a couple of days back restore the usual plan.
        away = UsageModel(history(lambda d, age: [(9, 17)] if age > 10 else [], 90), now, 300)
        self.assertEqual(away.plan(TODAY)[0], [])
        back = UsageModel(
            history(lambda d, age: [(9, 17)] if d.weekday() < 5 and not 2 < age <= 12 else [], 90),
            now,
            300,
        )
        self.assertEqual(len(back.plan(TODAY)[0]), 1)

    def test_light_usage_and_times_the_app_is_off_need_no_starts(self):
        now = local_ts(TODAY, 1)
        light = history(weekdays((10, 11)))
        self.assertTrue(UsageModel(light, now, 300).plan(TODAY)[0])
        # Calibrated against reported usage, these sessions use a fraction of a window.
        calibrated = UsageModel(light, now, 300, capacity=60.0)
        self.assertTrue(calibrated.calibrated)
        self.assertEqual(calibrated.plan(TODAY)[0], [])

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

    def test_planned_starts_run_once_only_when_enabled_and_custom_times_follow_usage_days(self):
        now = local_ts(TODAY, 5)
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
            self.assertEqual(following["active_until"], start + 60 + 18000)
            self.assertEqual(following["plan"]["date"], "2026-11-13")
            friday = following["plan"]["starts"][0]
            self.assertEqual(
                following["next_activation_at"],
                local_ts(TODAY + timedelta(days=1), *map(int, friday.split(":"))),
            )

            # Fixed times apply on days the agent is used: Friday 14:00, then Monday, not Saturday.
            scheduler.set_schedule("claude", {"enabled": True, "times": ["14:00"]})
            two = local_ts(TODAY, 14)
            snap = scheduler.snapshot(now=two - 600)["agents"]["claude"]
            self.assertEqual(
                (snap["plan"]["source"], snap["plan"]["starts"]), ("custom", ["14:00"])
            )
            activate.return_value = started(two + 60)
            self.assertEqual(scheduler.tick(now=two + 60)[0]["mode"], "custom")
            self.assertEqual(activate.await_count, 2)
            friday_evening = local_ts(TODAY + timedelta(days=1), 20)
            self.assertEqual(
                scheduler.snapshot(now=friday_evening)["agents"]["claude"]["next_activation_at"],
                local_ts(TODAY + timedelta(days=4), 14),
            )

            # The app records when it is running, for later plans.
            self.assertTrue(scheduler.online["2026-11-12"])
            restarted = Scheduler(self.usage, self.limits, self.config)
            self.assertEqual(restarted.online, scheduler.online)
            self.assertEqual(len(restarted.history), 2)

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

    def test_failures_retry_after_backoff_within_grace_and_hide_private_details(self):
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
            activate.side_effect = None
            activate.return_value = dict(activated=True, reason="activated", windows=windows)
            self.assertEqual(
                scheduler.tick(now=now + 30 + BACKOFF_SECONDS)[0]["status"], "activated"
            )
            self.assertEqual(activate.await_count, 2)

            for error, expected in (
                (FileNotFoundError("/secret/path"), "not_found"),
                (TimeoutError("secret"), "timeout"),
                (AgentUnavailable("login_required"), "login_required"),
            ):
                activate.side_effect = error
                result = scheduler.activate("claude", now=now + 20000)
                self.assertEqual(result["reason"], expected)
                self.assertNotIn("secret", json.dumps(result))

    def test_capacity_is_calibrated_from_reported_usage_for_the_next_day(self):
        now = local_ts(TODAY, 15)
        rows = history(weekdays((9, 17))) + [
            {"agent": "claude", "start": now - 3600 + minute * 60, "tokens": None}
            for minute in range(0, 60, 6)
        ]
        limits = {
            "agents": [
                {
                    "id": "claude",
                    "windows": [{"minutes": 300, "used_percent": 50, "resets_at": now + 7200}],
                }
            ]
        }
        scheduler = self.scheduler(rows)
        snap = scheduler.snapshot(now=now, limits_snapshot=limits)["agents"]["claude"]
        self.assertFalse(snap["model"]["calibrated"])
        self.assertEqual(list(scheduler.state["claude"]["capacity"].values()), [20.0])
        tomorrow = scheduler.snapshot(now=now + 86400, limits_snapshot={"agents": []})
        self.assertTrue(tomorrow["agents"]["claude"]["model"]["calibrated"])
        self.assertEqual(scheduler.models["claude"][1].capacity, 20.0)

    def test_demo_activation_uses_no_agent_process(self):
        now = local_ts(TODAY, 10)
        with patch.object(Codex, "activate_window", new_callable=AsyncMock) as live:
            scheduler = self.scheduler(history(weekdays((9, 17)), agent="codex"), DemoLimits())
            self.assertEqual(scheduler.activate("codex", now=now)["status"], "activated")
            self.assertEqual(scheduler.activate("codex", now=now + 60)["status"], "skipped_active")
            live.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
