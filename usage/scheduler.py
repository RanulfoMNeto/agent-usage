"""Window scheduler: learn when each agent is used and start its short usage window early
enough that resets land inside the work. Deciding costs no tokens; only a start does.

The pipeline runs locally and is rebuilt once per day:

1. Data. Prompt start times and token counts from local history, in 30-minute slots per
   calendar day. Days without prompts are observations too.
2. Prediction. Three forecasters weigh past days differently: the same weekday over months
   (weekly), the same kind of day over weeks (routine) and the last few days (recent). Every
   past day scores how well each would have predicted it, and an online mixture (exponential
   weights with a fixed share) moves trust to whichever explains current behavior. A new
   routine takes over within a few days; a single odd day barely matters.
3. Optimization. The forecast is a weighted set of plausible days. For a set of start times,
   the exact window chain is simulated on each plausible day and scored with a saturating
   utility per window (a window absorbs at most its quota), minus a small cost for every
   start that actually runs. Greedy selection with local moves picks the plan.
4. Recommendation. One plan per date, limited to times the app is usually running, with the
   window capacity calibrated against the usage percentage the agent reports.
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
from statistics import median

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
MAX_STARTS = 4
MAX_SCENARIOS = 60
ONLINE_DAYS = 28
CAPACITY_SAMPLES = 30
OBSERVE_SECONDS = 60
GRACE_SECONDS = 900
BACKOFF_SECONDS = 300
MAX_HISTORY = 50
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


def demand(row):
    """Quota pressure of one prompt: heavy prompts weigh more without cache reads dominating."""
    tokens = number(row.get("tokens"))
    return 1.0 + math.log1p(tokens / 10000.0) if tokens else 1.0


def usage_days(rows, now, lookback=LOOKBACK_DAYS):
    """Complete local days before today as (date, {slot: demand}), days without use included."""
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
        slots[slot] = slots.get(slot, 0.0) + demand(row)
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


def _natural_loads(days, span):
    """Demand per window when windows only start with the user's own prompts."""
    loads = []
    for _, slots in days:
        end, load = -1, 0.0
        for slot in sorted(slots):
            if slot >= end:
                if load:
                    loads.append(load)
                end, load = slot + span, 0.0
            load += slots[slot]
        if load:
            loads.append(load)
    return loads


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


def _utility(events, starts, span, capacity):
    """Exact window chain on one day: saturating utility and starts that opened a window."""
    total, fired, end, load = 0.0, 0, -1, 0.0
    i = j = 0
    while i < len(events) or j < len(starts):
        if j < len(starts) and (i >= len(events) or starts[j] <= events[i][0]):
            slot, amount = starts[j], 0.0
            j += 1
            opens = slot >= end
            fired += opens
        else:
            slot, amount = events[i]
            i += 1
            opens = slot >= end
        if opens:
            total += 1.0 - math.exp(-load / capacity)
            end, load = slot + span, 0.0
        load += amount
    return total + 1.0 - math.exp(-load / capacity), fired


def _score(scenarios, starts, span, capacity):
    value = 0.0
    for weight, events in scenarios:
        utility, fired = _utility(events, starts, span, capacity)
        value += weight * (utility - ACTIVATION_COST * fired)
    return value


def optimize(scenarios, span, capacity, allowed):
    """Start slots maximizing expected utility over plausible days, and the gain over none."""
    base = _score(scenarios, [], span, capacity)
    chosen, best = [], base
    candidates = [slot for slot in range(SLOTS) if allowed[slot]]
    for _ in range(MAX_STARTS):
        pick = None
        for slot in candidates:
            if slot in chosen:
                continue
            trial = sorted([*chosen, slot])
            value = _score(scenarios, trial, span, capacity)
            if value > best + 1e-9:
                best, pick = value, trial
        if pick is None:
            break
        chosen = pick
    improved = True
    while improved and chosen:
        improved = False
        for index in range(len(chosen)):
            rest = chosen[:index] + chosen[index + 1 :]
            for move in (-2, -1, 1, 2, None):
                if move is None:
                    trial = rest
                else:
                    slot = chosen[index] + move
                    if not 0 <= slot < SLOTS or not allowed[slot] or slot in rest:
                        continue
                    trial = sorted([*rest, slot])
                value = _score(scenarios, trial, span, capacity)
                if value > best + 1e-9:
                    best, chosen, improved = value, trial, True
                    break
            if improved:
                break
    return chosen, best - base


class UsageModel:
    """An agent's learned usage pattern as of one day, with plans computed per date on demand."""

    def __init__(self, rows, now, window_minutes, capacity=None, online=None):
        self.today = _local(now).date()
        self.window_minutes = window_minutes
        self.span = max(1, round(window_minutes / SLOT_MINUTES))
        self.days = usage_days(rows, now)
        self.active_days = sum(1 for _, slots in self.days if slots)
        self.trust = learn(self.days)
        loads = _natural_loads(self.days, self.span)
        self.calibrated = capacity is not None
        self.capacity = capacity or (median(loads) if loads else 1.0)
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
        """Learned start minutes for a date and their expected utility gain."""
        if target not in self._plans:
            starts, gain = [], 0.0
            if self.ready:
                slots, gain = optimize(
                    self.forecast(target)[2], self.span, self.capacity, self.allowed
                )
                starts = [slot * SLOT_MINUTES for slot in slots]
            self._plans[target] = (starts, gain)
        return self._plans[target]

    def starts(self, schedule, target):
        """Start minutes on a date: learned, or fixed times on days the agent is usually used."""
        if schedule["times"] is None:
            return self.plan(target)[0]
        if self.ready and self.forecast(target)[1] < 0.5:
            return []
        kept, end = [], -1
        for minutes in map(_minutes, schedule["times"]):
            if minutes >= end:
                kept.append(minutes)
                end = minutes + self.window_minutes
        return kept


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
    exhausted = any(w["used_percent"] >= 100 for w in windows)

    active, active_until = False, None
    in_use = [w for w in short_windows if w["used_percent"] > 0]
    if in_use:
        active, active_until = True, min(w["resets_at"] for w in in_use)
    elif (
        number(state.get("active_until")) is not None
        and state["active_until"] > now
        and now - (number(state.get("last_activated_at")) or 0) < window_minutes * 60
    ):
        active, active_until = True, float(state["active_until"])
    elif not short_windows:
        recent = [
            r["start"]
            for r in (rows or [])
            if isinstance(r, dict)
            and number(r.get("start")) is not None
            and now - window_minutes * 60 < r["start"] <= now
        ]
        if recent:
            active, active_until = True, min(recent) + window_minutes * 60
    return {
        "issue": issue,
        "disconnected": disconnected,
        "unavailable": unavailable,
        "weekly_exhausted": exhausted,
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
        self.models, self.keys, self.observed = {}, {}, {}
        self.rows_cache = None
        self.dirty = False
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
                self.state = {
                    k: v for k, v in states.items() if k in self.agents and isinstance(v, dict)
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

    def _capacity(self, agent_id, before):
        """Median window capacity from windows that reset before a time; callers hold the lock."""
        samples = (self.state.get(agent_id) or {}).get("capacity")
        if not isinstance(samples, dict):
            return None
        values = [
            v
            for k, v in samples.items()
            if k.isdigit() and int(k) * 600 < before and number(v) and v > 0
        ]
        return median(values) if values else None

    def _observe(self, agent_id, entry, rows, now):
        """Calibrate window capacity: local demand in the current window against its usage."""
        if now - self.observed.get(agent_id, -math.inf) < OBSERVE_SECONDS:
            return
        self.observed[agent_id] = now
        for window in (entry or {}).get("windows") or []:
            minutes, used = number(window.get("minutes")), number(window.get("used_percent"))
            resets_at = number(window.get("resets_at"))
            if None in (minutes, used, resets_at) or minutes >= 10080 or used < 10:
                continue
            if resets_at <= now:
                continue
            opened = resets_at - minutes * 60
            load = sum(
                demand(row)
                for row in rows
                if number(row.get("start")) is not None and opened <= row["start"] <= now
            )
            if load <= 0:
                continue
            estimate = round(load * 100 / used, 3)
            key = str(int(resets_at // 600))
            with self.lock:
                samples = self.state.setdefault(agent_id, {}).setdefault("capacity", {})
                previous = samples.get(key)
                if previous is None or abs(previous - estimate) > 0.05 * previous:
                    samples[key] = estimate
                    for old in sorted(samples, key=int)[:-CAPACITY_SAMPLES]:
                        samples.pop(old)
                    self.dirty = True

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
            capacity = self._capacity(agent.id, midnight)
            online = dict(self.online)
        key = (today, history_key, capacity)
        with self.model_lock:
            cached = self.models.get(agent.id)
            if cached and cached[0] == key:
                return cached[1]
            model = UsageModel(
                rows, now, int(getattr(agent, "window_minutes", 300)), capacity, online
            )
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

    def _slots(self, agent_id, model, schedule, first, count):
        """Planned activations as (timestamp, key) from a date onward, in order."""
        for offset in range(count):
            day = first + timedelta(days=offset)
            for minutes in model.starts(schedule, day):
                yield _at(day, minutes), f"{agent_id}:{day.isoformat()}:{_clock(minutes)}"

    def _pending(self, agent_id, model, schedule, state, now, days=9):
        """Planned activations not yet handled, including any still inside its grace period."""
        last_slot = state.get("last_slot")
        last_at = number(state.get("last_slot_at")) or 0.0
        for stamp, key in self._slots(
            agent_id, model, schedule, model.today - timedelta(days=1), days
        ):
            if stamp >= now - GRACE_SECONDS and key != last_slot and stamp > last_at:
                yield stamp, key

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

    def activate(self, agent_id, source="learned", slot=None, now=None):
        """Start a window unless one is active, the agent is unavailable or limits are spent."""
        if agent_id not in self.agents:
            raise ValueError("Unknown agent")
        now = time.time() if now is None else float(now)
        agent = self.agents[agent_id]

        def finish(status, reason, resets_at=None, handled=True, **changes):
            with self.lock:
                agent_state = self.state.setdefault(agent_id, {})
                agent_state.update(changes)
                if slot and handled:
                    agent_state["last_slot"], agent_state["last_slot_at"] = slot
                event = self._record(agent_id, now, source, status, reason, resets_at)
                self._save()
            return event

        if self._connections().get(agent_id) is False:
            return finish("skipped_disconnected", "disconnected")
        rows = self._rows().get(agent_id, [])
        with self.lock:
            current = dict(self.state.get(agent_id) or {})
        info = window_state(agent, self._limit_entries().get(agent_id), rows, current, now=now)
        if info["unavailable"]:
            reason = info.get("issue") or "query_failed"
            return finish("skipped_unavailable", reason, handled=False, last_error_at=now)
        if info["weekly_exhausted"]:
            return finish("skipped_exhausted", "weekly_exhausted")
        if info["active"]:
            return finish("skipped_active", "already_active", info["active_until"])

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
            return finish("failed", reason, handled=False, last_error_at=now)

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
        resets_at = min(resets) if resets else (now + info["window_minutes"] * 60)
        if not activated:
            status = "skipped_exhausted" if reason == "weekly_exhausted" else "skipped_active"
            return finish(status, reason, resets_at if resets else None)
        return finish(
            "activated",
            reason,
            resets_at,
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
        """Run every planned activation that is due for enabled, connected agents."""
        now = time.time() if now is None else float(now)
        self._mark_online(now)
        triggered = []
        connections = self._connections()
        grouped = None
        for agent_id, agent in self.agents.items():
            schedule = self.get_schedule(agent_id)
            if not schedule["enabled"] or connections.get(agent_id) is False:
                continue
            grouped = self._rows() if grouped is None else grouped
            model = self._model(agent, grouped.get(agent_id, []), now)
            with self.lock:
                state = dict(self.state.get(agent_id) or {})
            error_at = number(state.get("last_error_at")) or 0.0
            if error_at and now < error_at + BACKOFF_SECONDS:
                continue
            due = None
            for stamp, key in self._pending(agent_id, model, schedule, state, now, days=2):
                if stamp > now:
                    break
                due = (key, stamp)
            if due:
                source = "learned" if schedule["times"] is None else "custom"
                triggered.append(self.activate(agent_id, source=source, slot=due, now=now))
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
            schedules = {a: dict(self.schedules.get(a) or DEFAULT_SCHEDULE) for a in self.agents}
            states = {a: dict(self.state.get(a) or {}) for a in self.agents}
            history = list(self.history)
        result = {}
        for agent_id, agent in self.agents.items():
            schedule, state, rows = schedules[agent_id], states[agent_id], grouped.get(agent_id, [])
            self._observe(agent_id, entries.get(agent_id), rows, now)
            model = self._model(agent, rows, now)
            info = window_state(agent, entries.get(agent_id), rows, state, now=now)
            upcoming = next(self._pending(agent_id, model, schedule, state, now), None)
            shown = _local(upcoming[0]).date() if upcoming else model.today
            starts = model.starts(schedule, shown)
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
                if upcoming
                else "idle"
            )
            events = [item for item in history if item.get("agent") == agent_id]
            result[agent_id] = {
                "id": agent_id,
                "schedule": schedule,
                "status": status,
                "reason": reason,
                "window_minutes": info["window_minutes"],
                "next_activation_at": upcoming[0] if upcoming and status == "scheduled" else None,
                "active_until": info["active_until"] if info["active"] else None,
                "last_activated_at": state.get("last_activated_at"),
                "last_event": events[-1] if events else None,
                "plan": {
                    "date": shown.isoformat(),
                    "today": shown == model.today,
                    "source": "learned" if schedule["times"] is None else "custom",
                    "starts": [_clock(minutes) for minutes in starts],
                    "activity": [round(v, 3) for v in model.forecast(shown)[0]],
                },
                "model": {
                    "days": len(model.days),
                    "active_days": model.active_days,
                    "ready": model.ready,
                    "calibrated": model.calibrated,
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
                if self.stop_event.wait(self.refresh_seconds):
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
    "learn",
    "optimize",
    "usage_days",
    "validate_schedule",
    "window_state",
]
