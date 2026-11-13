"""Window scheduler: learn when each agent is used and start its short usage window early
enough that resets land inside the work. Deciding costs no tokens; only a start does.

The pipeline runs locally and is rebuilt once per day:

1. Data. Prompt start times from local history, in 30-minute slots per calendar day. Days
   without prompts are observations too.
2. Prediction. Three forecasters weigh past days differently: the same weekday over months
   (weekly), the same kind of day over weeks (routine) and the last few days (recent). Every
   past day scores how well each would have predicted it, and an online mixture (exponential
   weights with a fixed share) moves trust to whichever explains current behavior. A new
   routine takes over within a few days; a single odd day barely matters.
3. Optimization. The goal is the most usage the limit allows: every window brings a fresh
   quota, all of it usable while working in it, so the best start makes the most windows
   fall on work, with resets inside it rather than after it. The forecast is a weighted set
   of plausible days. For each candidate start time, the chain the scheduler runs is
   simulated on each plausible day and scored by the windows that hold any use, minus a small
   cost for every window the scheduler opens. The best start wins even when it gains nothing
   over starting none: every date gets one.
4. Recommendation. One start per date, preferring times the app is usually running.
5. Execution. From the day's start until midnight, whenever no window is open one is started:
   at the start itself, as soon as the app is running when the start was missed, and right
   when a window ends. A window still open at the start defers it to that window's reset.
   A window opened earlier that day, by the user's own prompt, another device or anything
   else, begins the chain instead: from its reset on, windows are renewed until midnight.
"""

import asyncio
import logging
import math
import re
import threading
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from datetime import time as clock
from pathlib import Path

from .agents import AGENTS, AgentUnavailable
from .records import number

SLOT_MINUTES = 30
SLOTS = 24 * 60 // SLOT_MINUTES
LOOKBACK_DAYS = 120
SCORED_DAYS = 56
WARMUP_DAYS = 7
MIN_ACTIVE_DAYS = 3
FORECASTERS = ("weekly", "routine", "recent")
HALF_LIFE_DAYS = (42.0, 21.0, 3.0)
LEARNING_RATE = 0.5
FIXED_SHARE = 0.05
PRIOR_DAYS = 0.5
ACTIVATION_COST = 0.02
MAX_SCENARIOS = 60
ONLINE_DAYS = 28
BACKOFF_SECONDS = 300
SETTLE_SECONDS = 30
LOOKAHEAD_DAYS = 9
ON_TIME_SECONDS = 120
KEEP_WINDOWS_SECONDS = 2 * 86400
MAX_WINDOWS = 16
MAX_HISTORY = 50
START_OUTCOMES = {"activated": "ran", "skipped_active": "deferred", "failed": "failed"}
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_SCHEDULE = {"enabled": False, "times": None}


def validate_schedule(raw):
    """One agent's settings: on or off, and learned (None) or fixed start times."""
    if not isinstance(raw, dict) or set(raw) - {"enabled", "times"}:
        raise ValueError("Invalid schedule settings")
    enabled = raw.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("Invalid schedule settings")
    times = raw.get("times")
    if times is not None:
        if (
            not isinstance(times, list)
            or not 1 <= len(times) <= 8
            or any(not isinstance(t, str) or not TIME_PATTERN.match(t) for t in times)
        ):
            raise ValueError("Invalid schedule times")
        times = sorted(set(times))
    return {"enabled": enabled, "times": times}


def stored_schedule(value):
    """Saved settings, accepting the earlier mode-based format."""
    if isinstance(value, dict) and "mode" in value:
        mode = value.get("mode")
        value = {
            "enabled": mode in ("auto", "scheduled", "smart"),
            "times": value.get("times") if mode == "scheduled" else None,
        }
    return validate_schedule(value)


def _local(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone()


def _at(day, minutes):
    """Timestamp of a local wall-clock minute on a date."""
    return datetime.combine(day, clock(minutes // 60, minutes % 60)).astimezone().timestamp()


def _minutes(text):
    hours, minutes = map(int, text.split(":"))
    return hours * 60 + minutes


def _clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def usage_days(rows, now, lookback=LOOKBACK_DAYS):
    """Complete local days before today as (date, {slot: prompts}), days without use included."""
    today = _local(now).date()
    buckets = {}
    for row in rows or ():
        start = number(row.get("start")) if isinstance(row, dict) else None
        if start is None or start > now:
            continue
        moment = _local(start)
        if not 1 <= (today - moment.date()).days <= lookback:
            continue
        slot = (moment.hour * 60 + moment.minute) // SLOT_MINUTES
        slots = buckets.setdefault(moment.date(), {})
        slots[slot] = slots.get(slot, 0) + 1
    if not buckets:
        return []
    first = min(buckets)
    return [
        (first + timedelta(days=offset), buckets.get(first + timedelta(days=offset), {}))
        for offset in range((today - first).days)
    ]


def _kernels(target, day):
    """How much each forecaster relies on one past day when predicting the target date."""
    if (target.weekday() >= 5) != (day.weekday() >= 5):
        return (0.0, 0.0, 0.0)
    age = (target - day).days
    weekly = 0.5 ** (age / HALF_LIFE_DAYS[0]) if target.weekday() == day.weekday() else 0.0
    return (weekly, 0.5 ** (age / HALF_LIFE_DAYS[1]), 0.5 ** (age / HALF_LIFE_DAYS[2]))


def _forecasters(days, target, prior):
    """Per forecaster: activity probability per slot, its weights over past days, total weight."""
    sums = [[0.0] * SLOTS for _ in FORECASTERS]
    picks = [[] for _ in FORECASTERS]
    masses = [0.0] * len(FORECASTERS)
    for index, (day, slots) in enumerate(days):
        for forecaster, weight in enumerate(_kernels(target, day)):
            if weight < 1e-4:
                continue
            masses[forecaster] += weight
            picks[forecaster].append((index, weight))
            total = sums[forecaster]
            for slot in slots:
                total[slot] += weight
    probabilities = [
        [(sums[f][s] + PRIOR_DAYS * prior[s]) / (masses[f] + PRIOR_DAYS) for s in range(SLOTS)]
        for f in range(len(FORECASTERS))
    ]
    return probabilities, picks, masses


def learn(days):
    """Online mixture weights: every complete day scores the forecasters that predicted it."""
    trust = [1.0 / len(FORECASTERS)] * len(FORECASTERS)
    counts, seen = [0] * SLOTS, 0
    first = max(WARMUP_DAYS, len(days) - SCORED_DAYS)
    for index, (day, slots) in enumerate(days):
        if index >= first:
            prior = [count / seen for count in counts]
            probabilities, _, _ = _forecasters(days[:index], day, prior)
            # Brier score over the day's slots: timing errors and missed or extra use both count.
            losses = [
                sum((p[s] - (1.0 if s in slots else 0.0)) ** 2 for s in range(SLOTS))
                for p in probabilities
            ]
            best = min(losses)
            trust = [t * math.exp(-LEARNING_RATE * (loss - best)) for t, loss in zip(trust, losses)]
            total = sum(trust)
            # A fixed share keeps every forecaster ready to take over when habits change.
            trust = [(1 - FIXED_SHARE) * t / total + FIXED_SHARE / len(trust) for t in trust]
        for slot in slots:
            counts[slot] += 1
        seen += 1
    return trust


def availability(online, today):
    """Recency-weighted share of recent days the app was running in each slot."""
    tracked = []
    for key, mask in (online or {}).items():
        try:
            day = date.fromisoformat(key)
        except (TypeError, ValueError):
            continue
        if type(mask) is int and 1 <= (today - day).days <= ONLINE_DAYS:
            tracked.append(day)
    # Without a few days of evidence, any time is assumed reachable.
    if not tracked or (today - min(tracked)).days < 3:
        return [1.0] * SLOTS
    totals, mass = [0.0] * SLOTS, 0.0
    for offset in range(1, (today - min(tracked)).days + 1):
        weight = 0.5 ** (offset / 10)
        mask = online.get((today - timedelta(days=offset)).isoformat(), 0)
        mass += weight
        for slot in range(SLOTS):
            if mask >> slot & 1:
                totals[slot] += weight
    return [(totals[slot] + 1.0) / (mass + 1.0) for slot in range(SLOTS)]


def _utility(prompts, first, span):
    """Exact window chain on one day: windows that hold any use, each a fresh quota usable in
    full while working in it, and windows the scheduler opened.

    Until the chain begins only prompts open windows. It begins at the day's start, or with the
    first window when one opens earlier; from then until midnight the scheduler opens one
    whenever none is open.
    """
    used, fired, end, busy = 0, 0, -1, False
    for slot in range(SLOTS):
        chained = first is not None and (slot >= first or end >= 0)
        if slot >= end and (chained or slot in prompts):
            used += busy
            end, busy = slot + span, False
            fired += chained
        busy = busy or slot in prompts
    return used + busy, fired


def optimize(scenarios, span, allowed):
    """The day's start slot making the most windows fall on work over plausible days, and its
    gain over starting none, which may be negative: a day always gets a start, at a time the
    app is usually running when there is one, and the earliest of equally good slots."""
    days = [(weight, dict(events)) for weight, events in scenarios]

    def score(first):
        value = 0.0
        for weight, prompts in days:
            used, fired = _utility(prompts, first, span)
            value += weight * (used - ACTIVATION_COST * fired)
        return value

    candidates = [slot for slot in range(SLOTS) if allowed[slot]] or range(SLOTS)
    chosen, best = None, -math.inf
    for slot in candidates:
        value = score(slot)
        if value > best + 1e-9:
            chosen, best = slot, value
    return [chosen], best - score(None)


class UsageModel:
    """An agent's learned usage pattern as of one day, with plans computed per date on demand."""

    def __init__(self, rows, now, window_minutes, online=None):
        self.today = _local(now).date()
        self.window_minutes = window_minutes
        self.span = max(1, round(window_minutes / SLOT_MINUTES))
        self.days = usage_days(rows, now)
        self.active_days = sum(1 for _, slots in self.days if slots)
        self.trust = learn(self.days)
        self.allowed = [share >= 0.5 for share in availability(online, self.today)]
        counts = [0] * SLOTS
        for _, slots in self.days:
            for slot in slots:
                counts[slot] += 1
        self.prior = [count / len(self.days) for count in counts] if self.days else counts
        self._forecasts, self._plans = {}, {}

    @property
    def ready(self):
        return self.active_days >= MIN_ACTIVE_DAYS

    def forecast(self, target):
        """Activity probability per slot, chance of any use, and weighted plausible days."""
        if target not in self._forecasts:
            probabilities, picks, masses = _forecasters(self.days, target, self.prior)
            activity = [
                sum(t * p[slot] for t, p in zip(self.trust, probabilities)) for slot in range(SLOTS)
            ]
            usable = [f for f in range(len(FORECASTERS)) if masses[f] > 0]
            share = sum(self.trust[f] for f in usable)
            weights = {}
            for f in usable:
                for index, weight in picks[f]:
                    weights[index] = (
                        weights.get(index, 0.0) + self.trust[f] / share * weight / masses[f]
                    )
            ranked = sorted(weights.items(), key=lambda item: -item[1])[:MAX_SCENARIOS]
            total = sum(weight for _, weight in ranked)
            scenarios = [
                (weight / total, sorted(self.days[index][1].items())) for index, weight in ranked
            ]
            busy = sum(weight for weight, events in scenarios if events)
            self._forecasts[target] = (activity, busy, scenarios)
        return self._forecasts[target]

    def plan(self, target):
        """Learned start minute for a date (one once the model is ready, none while learning)
        and its expected utility gain, which may be negative."""
        if target not in self._plans:
            starts, gain = [], 0.0
            if self.ready:
                slots, gain = optimize(self.forecast(target)[2], self.span, self.allowed)
                starts = [slot * SLOT_MINUTES for slot in slots]
            self._plans[target] = (starts, gain)
        return self._plans[target]

    def start(self, schedule, target):
        """Minute a date's chain starts: learned, or the earliest fixed time, every day; None
        only while a learned plan is still learning."""
        if schedule["times"] is None:
            starts = self.plan(target)[0]
            return starts[0] if starts else None
        return _minutes(schedule["times"][0])


def _windows(saved):
    """Kept short windows from saved state, ignoring malformed entries."""
    return [
        {"start": float(w["start"]), "end": float(w["end"]), "by": w.get("by", "you")}
        for w in (saved if isinstance(saved, list) else [])
        if isinstance(w, dict) and number(w.get("start")) is not None and number(w.get("end"))
    ]


def first_window(windows, prompts, day, now, seconds):
    """When a day's first window opened, up to now; None before any.

    Known windows are the ones the agent reported or the scheduler started. A prompt outside
    every window opens one, as the agent does after a reset, so a window that opened and ended
    while the app was not running still counts. Windows that opened the day before never do.
    """
    midnight = _at(day, 0)
    known = [(w["start"], w["end"]) for w in windows if w["start"] <= now]
    opened = [start for start, _ in known if start >= midnight]
    end = -math.inf
    for moment in sorted(p for p in prompts if midnight - seconds <= p <= now):
        if moment < end or any(start <= moment < stop for start, stop in known):
            continue
        if moment >= midnight:
            opened.append(moment)
            break
        end = moment + seconds
    return min(opened, default=None)


def _start_status(start_at, events, now, opened=None):
    """How a day's start went: early when a window opened before it began the chain, otherwise
    pending, ran, deferred, failed, skipped or missed."""
    if opened is not None and opened < start_at:
        return "early"
    if start_at > now:
        return "pending"
    for event in events:
        stamp = number(event.get("timestamp"))
        if stamp is not None and start_at - 1 <= stamp <= start_at + ON_TIME_SECONDS:
            return START_OUTCOMES.get(event.get("status"), "skipped")
    return "missed"


def window_state(agent, limit_entry, rows, state, now=None):
    """Current short window, weekly exhaustion and connection status for one agent."""
    now = time.time() if now is None else float(now)
    entry = limit_entry if isinstance(limit_entry, dict) else {}
    state = state if isinstance(state, dict) else {}
    windows = [
        w
        for w in (entry.get("windows") or [])
        if isinstance(w, dict)
        and number(w.get("minutes")) is not None
        and number(w.get("used_percent")) is not None
        and number(w.get("resets_at")) is not None
        and w["resets_at"] > now
    ]
    window_minutes = int(getattr(agent, "window_minutes", 300))
    disconnected = entry.get("connected") is False or entry.get("issue") == "disconnected"
    issue = entry.get("issue")
    unavailable = not disconnected and not windows and bool(issue)
    short_windows = [w for w in windows if w["minutes"] < 10080]
    spent = [w["resets_at"] for w in windows if w["used_percent"] >= 100]
    known = number(state.get("active_until"))

    active, active_until = False, None
    in_use = [w for w in short_windows if w["used_percent"] > 0]
    if in_use:
        active, active_until = True, min(w["resets_at"] for w in in_use)
    elif known is not None and now < known <= now + window_minutes * 60:
        active, active_until = True, known
    elif not short_windows:
        # Prompts up to a reset the scheduler saw belong to windows that already ended.
        since = max(now - window_minutes * 60, known if known is not None else -math.inf)
        recent = [
            r["start"]
            for r in (rows or [])
            if isinstance(r, dict)
            and number(r.get("start")) is not None
            and since < r["start"] <= now
        ]
        if recent:
            active, active_until = True, min(recent) + window_minutes * 60
    return {
        "issue": issue,
        "disconnected": disconnected,
        "unavailable": unavailable,
        "weekly_exhausted": bool(spent),
        "exhausted_until": max(spent) if spent else None,
        "window_minutes": window_minutes,
        "active": active,
        "active_until": active_until,
    }


class Scheduler:
    """Per-agent window schedules: learning, planning, and activations when they are due."""

    def __init__(self, usage, limits, config=None, agents=AGENTS):
        self.usage = usage
        self.limits = limits
        self.agents = agents
        base = config or getattr(usage, "config", None) or {}
        settings = (base.get("scheduler") if isinstance(base, dict) else None) or {}
        self.refresh_seconds = (
            settings.get("refresh_seconds", 15) if isinstance(settings, dict) else 15
        )
        self.lock = threading.Lock()
        self.model_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None
        self.schedules, self.state, self.history, self.online = {}, {}, [], {}
        self.models, self.keys = {}, {}
        self.rows_cache = None
        self.dirty = False
        self.wake_at = None
        self._load()

    def _persistent(self):
        return isinstance(getattr(self.usage, "path", None), Path)

    def _load(self):
        if not self._persistent():
            return
        try:
            with closing(self.usage.connect()) as db:
                for agent_id, value in self.usage.setting(db, "schedules").items():
                    if agent_id in self.agents:
                        try:
                            self.schedules[agent_id] = stored_schedule(value)
                        except ValueError:
                            pass
                saved = self.usage.setting(db, "scheduler_state")
            states = saved.get("state")
            if isinstance(states, dict):
                # Slot markers belonged to the per-start scheduler and capacity samples to the
                # usage calibration; plans need neither.
                self.state = {
                    k: {
                        key: value
                        for key, value in v.items()
                        if not key.startswith("last_slot") and key != "capacity"
                    }
                    for k, v in states.items()
                    if k in self.agents and isinstance(v, dict)
                }
            history = saved.get("history")
            if isinstance(history, list):
                self.history = [
                    item
                    for item in history[-MAX_HISTORY:]
                    if isinstance(item, dict) and item.get("agent") in self.agents
                ]
            online = saved.get("online")
            if isinstance(online, dict):
                self.online = {
                    k: v
                    for k, v in online.items()
                    if isinstance(k, str) and type(v) is int and 0 <= v < 1 << SLOTS
                }
        except Exception as exc:
            logging.warning("Scheduler state could not be loaded (%s)", type(exc).__name__)

    def _save(self):
        """Persist settings and state; callers hold the lock."""
        self.dirty = False
        if not self._persistent():
            return
        self.usage.store("schedules", self.schedules)
        self.usage.store(
            "scheduler_state",
            {"state": self.state, "history": self.history[-MAX_HISTORY:], "online": self.online},
        )

    def _rows(self):
        """Prompt rows grouped by agent, reused while the collection is unchanged."""
        generation = getattr(self.usage, "generation", None)
        cacheable = isinstance(generation, int)
        if cacheable and self.rows_cache and self.rows_cache[0] == generation:
            return self.rows_cache[1]
        try:
            snap = self.usage.snapshot()
            rows = snap.get("rows") if isinstance(snap, dict) else None
            if not isinstance(rows, list):
                return {}
        except Exception:
            return {}
        grouped = {}
        for row in rows:
            if isinstance(row, dict):
                grouped.setdefault(row.get("agent"), []).append(row)
        if cacheable:
            self.rows_cache = (generation, grouped)
        return grouped

    def _connections(self):
        connections = getattr(self.usage, "connections", None)
        return connections if isinstance(connections, dict) else {}

    def _limit_entries(self, limits_snapshot=None):
        try:
            snap = (
                limits_snapshot
                if isinstance(limits_snapshot, dict)
                else self.limits.snapshot(self._connections())
            )
            entries = snap.get("agents") if isinstance(snap, dict) else []
            if not isinstance(entries, list):
                return {}
            return {e["id"]: e for e in entries if isinstance(e, dict) and "id" in e}
        except Exception:
            return {}

    def _model(self, agent, rows, now):
        """The agent's model for today, rebuilt when the day or earlier history changes."""
        today = _local(now).date()
        midnight = _at(today, 0)
        generation = getattr(self.usage, "generation", None)
        cached = self.keys.get(agent.id)
        if isinstance(generation, int) and cached and cached[0] == (generation, today):
            history_key = cached[1]
        else:
            starts = [
                row["start"]
                for row in rows
                if number(row.get("start")) is not None and row["start"] < midnight
            ]
            history_key = (len(starts), max(starts, default=0.0))
            self.keys[agent.id] = ((generation, today), history_key)
        with self.lock:
            online = dict(self.online)
        key = (today, history_key)
        with self.model_lock:
            cached = self.models.get(agent.id)
            if cached and cached[0] == key:
                return cached[1]
            model = UsageModel(rows, now, int(getattr(agent, "window_minutes", 300)), online)
            self.models[agent.id] = (key, model)
            return model

    def get_schedule(self, agent_id):
        if agent_id not in self.agents:
            raise ValueError("Unknown agent")
        with self.lock:
            return dict(self.schedules.get(agent_id) or DEFAULT_SCHEDULE)

    def set_schedule(self, agent_id, values):
        if agent_id not in self.agents:
            raise ValueError("Unknown agent")
        validated = validate_schedule(values)
        with self.lock:
            self.schedules[agent_id] = validated
            self.state.setdefault(agent_id, {})["last_error_at"] = 0
            self._save()
        return self.snapshot()

    @staticmethod
    def _opened(agent, state, rows, now):
        """When today's first window opened, from known windows and the agent's prompts."""
        prompts = [row["start"] for row in rows if number(row.get("start")) is not None]
        seconds = int(getattr(agent, "window_minutes", 300)) * 60
        return first_window(
            _windows(state.get("windows")), prompts, _local(now).date(), now, seconds
        )

    @staticmethod
    def _next_start(model, schedule, state, now, busy_until=None, opened=None):
        """When the next window should start: now when one is due, or later; None without one.

        Each day runs one chain from its start until midnight, or from today's first window
        (opened) when it opened earlier: a start missed while the app was not running is due as
        soon as it runs, and a start that finds a window open waits for its reset. A day's
        chain never carries into the next day.
        """
        error_at = number(state.get("last_error_at")) or 0.0
        ready = max(
            now,
            number(state.get("active_until")) or 0.0,
            number(state.get("blocked_until")) or 0.0,
            error_at + BACKOFF_SECONDS if error_at else 0.0,
            busy_until or 0.0,
        )
        for offset in range(LOOKAHEAD_DAYS):
            day = model.today + timedelta(days=offset)
            minutes = model.start(schedule, day)
            if minutes is None:
                continue
            origin = _at(day, minutes)
            if offset == 0 and opened is not None:
                origin = min(origin, opened)
            moment = max(origin, ready)
            if moment < _at(day + timedelta(days=1), 0):
                return moment
        return None

    def _record(self, agent_id, now, source, status, reason, resets_at=None):
        """Append an activation event; callers hold the lock."""
        event = {
            "agent": agent_id,
            "timestamp": now,
            "mode": source,
            "status": status,
            "reason": reason,
            "resets_at": resets_at,
        }
        self.history = [*self.history, event][-MAX_HISTORY:]
        return event

    def _remember(self, agent_id, end, seconds, by, now):
        """Keep a short window for the chart, from yesterday on; callers hold the lock.

        Windows never overlap, so one overlapping a kept window is that window: its reset is
        refreshed, and a window the scheduler started stays the scheduler's.
        """
        state = self.state.setdefault(agent_id, {})
        old = state.get("windows")
        kept = [dict(w) for w in _windows(old) if w["end"] > now - KEEP_WINDOWS_SECONDS]
        start = end - seconds
        same = next((w for w in kept if w["start"] < end and start < w["end"]), None)
        if same is None:
            kept.append({"start": start, "end": end, "by": by})
        else:
            origin = "scheduler" if "scheduler" in (by, same["by"]) else by
            same.update(start=start, end=end, by=origin)
        kept = sorted(kept, key=lambda w: w["start"])[-MAX_WINDOWS:]
        if kept != old:
            state["windows"] = kept
            self.dirty = True

    def _seen(self, agent_id, entry, now):
        """Remember the short windows in use the agent reports; callers hold the lock."""
        for window in (entry or {}).get("windows") or []:
            if not isinstance(window, dict):
                continue
            minutes, used = number(window.get("minutes")), number(window.get("used_percent"))
            resets_at = number(window.get("resets_at"))
            if None in (minutes, used, resets_at) or minutes >= 10080 or used <= 0:
                continue
            if resets_at > now:
                self._remember(agent_id, resets_at, minutes * 60, "you", now)

    def activate(self, agent_id, source="learned", now=None):
        """Start a window unless one is active, the agent is unavailable or limits are spent.

        Every outcome other than a start records when to try again: the open window's reset,
        the spent limit's reset, or a backoff after a failure.
        """
        if agent_id not in self.agents:
            raise ValueError("Unknown agent")
        now = time.time() if now is None else float(now)
        agent = self.agents[agent_id]
        window_seconds = int(getattr(agent, "window_minutes", 300)) * 60

        def finish(status, reason, resets_at=None, window=None, **changes):
            with self.lock:
                self.state.setdefault(agent_id, {}).update(changes)
                if window:
                    self._remember(agent_id, window[0], window_seconds, window[1], now)
                event = self._record(agent_id, now, source, status, reason, resets_at)
                self._save()
            return event

        if self._connections().get(agent_id) is False:
            return finish("skipped_disconnected", "disconnected")
        rows = self._rows().get(agent_id, [])
        entry = self._limit_entries().get(agent_id)
        with self.lock:
            self._seen(agent_id, entry, now)
            current = dict(self.state.get(agent_id) or {})
        info = window_state(agent, entry, rows, current, now=now)
        if info["unavailable"]:
            reason = info.get("issue") or "query_failed"
            return finish("skipped_unavailable", reason, last_error_at=now)
        if info["weekly_exhausted"]:
            return finish(
                "skipped_exhausted", "weekly_exhausted", blocked_until=info["exhausted_until"]
            )
        if info["active"]:
            until = min(info["active_until"], now + window_seconds)
            return finish("skipped_active", "already_active", until, active_until=until)

        try:
            if hasattr(self.limits, "activate"):
                outcome = self.limits.activate(agent, now=now)
            else:
                timeout = getattr(self.limits, "settings", {}).get("timeout_seconds", 10)
                outcome = asyncio.run(agent.activate_window(self.limits.command(agent), timeout))
        except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, AgentUnavailable)
                else "not_found"
                if isinstance(exc, FileNotFoundError)
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "query_failed"
            )
            return finish("failed", reason, last_error_at=now)

        windows = outcome.get("windows") or []
        if windows and isinstance(getattr(self.limits, "results", None), dict):
            with self.limits.lock:
                self.limits.results[agent_id] = dict(updated_at=now, windows=windows)
                if isinstance(getattr(self.limits, "checked", None), dict):
                    self.limits.checked[agent_id] = time.monotonic()
        activated = bool(outcome.get("activated"))
        reason = outcome.get("reason") or ("activated" if activated else "already_active")
        resets = [
            w["resets_at"]
            for w in windows
            if isinstance(w, dict)
            and number(w.get("minutes")) is not None
            and w["minutes"] < 10080
            and number(w.get("resets_at")) is not None
            and w["resets_at"] > now
        ]
        resets_at = min(min(resets), now + window_seconds) if resets else now + window_seconds
        if not activated and reason == "weekly_exhausted":
            spent = [
                w["resets_at"]
                for w in windows
                if isinstance(w, dict)
                and (number(w.get("used_percent")) or 0) >= 100
                and (number(w.get("resets_at")) or 0) > now
            ]
            blocked = max(spent) if spent else now + BACKOFF_SECONDS
            return finish("skipped_exhausted", reason, blocked_until=blocked)
        if not activated:
            # Wait for the open window's reset; without one ahead, the agent still counts a
            # window that just reset, so try again shortly.
            if not resets:
                return finish("skipped_active", reason, active_until=now + SETTLE_SECONDS)
            return finish(
                "skipped_active", reason, resets_at, (resets_at, "you"), active_until=resets_at
            )
        return finish(
            "activated",
            reason,
            resets_at,
            (resets_at, "scheduler"),
            last_activated_at=now,
            active_until=resets_at,
            last_error_at=0,
        )

    def _mark_online(self, now):
        """Remember that the app was running in this slot; plans prefer times it usually is."""
        moment = _local(now)
        key = moment.date().isoformat()
        bit = 1 << (moment.hour * 60 + moment.minute) // SLOT_MINUTES
        with self.lock:
            mask = self.online.get(key, 0)
            if not mask & bit:
                self.online[key] = mask | bit
                oldest = (moment.date() - timedelta(days=ONLINE_DAYS + 1)).isoformat()
                self.online = {k: v for k, v in self.online.items() if k >= oldest}
                self.dirty = True

    def tick(self, now=None):
        """Start a window for every enabled, connected agent whose chain is due, and note when
        the next one is due so the worker wakes right then."""
        now = time.time() if now is None else float(now)
        self.wake_at = None
        self._mark_online(now)
        triggered, upcoming = [], []
        connections = self._connections()
        grouped = entries = None
        for agent_id, agent in self.agents.items():
            schedule = self.get_schedule(agent_id)
            if not schedule["enabled"] or connections.get(agent_id) is False:
                continue
            grouped = self._rows() if grouped is None else grouped
            # Windows the agent reports, so one opened before the start begins the chain even
            # while nothing shows the widget.
            entries = self._limit_entries() if entries is None else entries
            rows = grouped.get(agent_id, [])
            model = self._model(agent, rows, now)
            with self.lock:
                self._seen(agent_id, entries.get(agent_id), now)
                state = dict(self.state.get(agent_id) or {})
            opened = self._opened(agent, state, rows, now)
            due = self._next_start(model, schedule, state, now, opened=opened)
            if due is not None and due <= now:
                source = "learned" if schedule["times"] is None else "custom"
                triggered.append(self.activate(agent_id, source=source, now=now))
                with self.lock:
                    state = dict(self.state.get(agent_id) or {})
                opened = self._opened(agent, state, rows, now)
                due = self._next_start(model, schedule, state, now, opened=opened)
            if due is not None:
                upcoming.append(due)
        self.wake_at = min(upcoming, default=None)
        with self.lock:
            if self.dirty:
                self._save()
        return triggered

    def snapshot(self, now=None, limits_snapshot=None):
        """Per agent: settings, status, next start and the plan the settings chart shows."""
        now = time.time() if now is None else float(now)
        entries = self._limit_entries(limits_snapshot)
        grouped = self._rows()
        with self.lock:
            for agent_id in self.agents:
                self._seen(agent_id, entries.get(agent_id), now)
            schedules = {a: dict(self.schedules.get(a) or DEFAULT_SCHEDULE) for a in self.agents}
            states = {a: dict(self.state.get(a) or {}) for a in self.agents}
            history = list(self.history)
        result = {}
        for agent_id, agent in self.agents.items():
            schedule, state, rows = schedules[agent_id], states[agent_id], grouped.get(agent_id, [])
            model = self._model(agent, rows, now)
            info = window_state(agent, entries.get(agent_id), rows, state, now=now)
            opened = self._opened(agent, state, rows, now)
            upcoming = self._next_start(
                model,
                schedule,
                state,
                now,
                info["active_until"] if info["active"] else None,
                opened,
            )
            shown = _local(upcoming).date() if upcoming is not None else model.today
            start = model.start(schedule, shown)
            reason = (
                "disconnected"
                if info["disconnected"]
                else "weekly_limit"
                if info["weekly_exhausted"]
                else "unavailable"
                if info["unavailable"]
                else None
            )
            status = (
                "off"
                if not schedule["enabled"]
                else "paused"
                if reason
                else "learning"
                if schedule["times"] is None and not model.ready
                else "scheduled"
            )
            events = [item for item in history if item.get("agent") == agent_id]
            # The chart's day in wall-clock minutes; windows may start before it or end after.
            begin, end = _at(shown, 0), _at(shown + timedelta(days=1), 0)
            midnight = datetime.combine(shown, clock())

            def minute(stamp, midnight=midnight):
                return round((datetime.fromtimestamp(stamp) - midnight).total_seconds() / 60)

            windows = [
                {
                    "start": minute(w["start"]),
                    "end": minute(w["end"]),
                    "by": w["by"],
                    "open": w["start"] <= now < w["end"],
                }
                for w in _windows(state.get("windows"))
                if w["start"] < end and w["end"] > begin
            ]
            today = [e for e in events if begin <= (number(e.get("timestamp")) or 0) < end]
            result[agent_id] = {
                "id": agent_id,
                "schedule": schedule,
                "status": status,
                "reason": reason,
                "window_minutes": info["window_minutes"],
                "next_activation_at": upcoming if status == "scheduled" else None,
                "active_until": info["active_until"] if info["active"] else None,
                "last_activated_at": state.get("last_activated_at"),
                "last_event": events[-1] if events else None,
                "plan": {
                    "date": shown.isoformat(),
                    "today": shown == model.today,
                    "source": "learned" if schedule["times"] is None else "custom",
                    "starts": [] if start is None else [_clock(start)],
                    "start_status": None
                    if start is None
                    else _start_status(
                        _at(shown, start), today, now, opened if shown == model.today else None
                    ),
                    "next": minute(upcoming) if status == "scheduled" else None,
                    "windows": windows,
                    "events": [
                        {"at": minute(e["timestamp"]), "status": e.get("status")} for e in today
                    ],
                    "activity": [round(v, 3) for v in model.forecast(shown)[0]],
                },
                "model": {
                    "days": len(model.days),
                    "active_days": model.active_days,
                    "ready": model.ready,
                    "trust": {
                        name: round(weight, 3) for name, weight in zip(FORECASTERS, model.trust)
                    },
                },
            }
        with self.lock:
            if self.dirty:
                self._save()
        return {"agents": result}

    def start(self):
        def watch():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    logging.warning("Scheduler check failed (%s)", type(exc).__name__)
                # Wake exactly when the next start is due, so a reset is followed at once.
                delay = self.refresh_seconds
                if self.wake_at is not None:
                    delay = min(delay, max(1.0, self.wake_at - time.time()))
                if self.stop_event.wait(delay):
                    break

        self.worker = threading.Thread(target=watch, daemon=True, name="agent-usage-scheduler")
        self.worker.start()

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=5)


__all__ = [
    "DEFAULT_SCHEDULE",
    "Scheduler",
    "UsageModel",
    "availability",
    "first_window",
    "learn",
    "optimize",
    "usage_days",
    "validate_schedule",
    "window_state",
]
