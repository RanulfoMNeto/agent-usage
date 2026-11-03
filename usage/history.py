"""Incremental native log ingestion and a portable local usage archive."""

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from .agents import AGENTS
from .records import estimate, number

FORMAT = "agent-usage"
VERSION = 3
MAX_IMPORT = 64 * 1024 * 1024
COMMON = {"id", "kind", "agent", "session", "turn", "updated"}
PROMPT = COMMON | {"start", "end", "title", "status", "duration_ms", "duration_exact"}
CALL = COMMON | {
    "model",
    "native_cost",
    "input",
    "output",
    "cached",
    "write",
    "reasoning",
    "counter",
}
UPSERT = """INSERT INTO records VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET updated=excluded.updated, data=excluded.data
    WHERE records.kind = excluded.kind AND records.counter IS excluded.counter
      AND excluded.updated >= records.updated AND excluded.data != records.data
    ON CONFLICT(counter) DO UPDATE SET id=excluded.id, updated=excluded.updated, data=excluded.data
    WHERE excluded.id != excluded.counter AND excluded.updated >= records.updated"""


def is_hash(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def upgrade(record, agents):
    """Earlier archives named agents by namespace and kept project paths."""
    if not isinstance(record, dict):
        return record
    names = {agent.namespace: agent.id for agent in agents.values()}
    record = {key: value for key, value in record.items() if key != "project"}
    record["agent"] = names.get(record.get("agent"), record.get("agent"))
    return record


def validate(record, agents):
    if not isinstance(record, dict) or record.get("kind") not in ("prompt", "call"):
        raise ValueError("Invalid history record")
    fields = PROMPT if record["kind"] == "prompt" else CALL
    if set(record) != fields or record["agent"] not in agents:
        raise ValueError("Invalid history fields")
    if not is_hash(record["id"]) or not is_hash(record["turn"]):
        raise ValueError("Invalid record identity")
    if not isinstance(record["session"], str) or not 1 <= len(record["session"]) <= 200:
        raise ValueError("Invalid session")
    for key in fields & {
        "updated",
        "start",
        "end",
        "duration_ms",
        "native_cost",
        "input",
        "output",
        "cached",
        "write",
        "reasoning",
    }:
        value = record[key]
        if value is None and key not in ("updated", "start"):
            continue
        if not isinstance(value, (int, float)) or number(value) is None or value > 1e16:
            raise ValueError("Invalid history number")
    if record["kind"] == "prompt":
        if record["id"] != record["turn"] or record["status"] not in (
            "observed",
            "completed",
            "interrupted",
        ):
            raise ValueError("Invalid prompt")
        if record["title"] is not None and (
            not isinstance(record["title"], str) or len(record["title"]) > 80
        ):
            raise ValueError("Invalid title")
        if not isinstance(record["duration_exact"], bool):
            raise ValueError("Invalid duration")
        if record["end"] is not None and record["end"] < record["start"]:
            raise ValueError("Invalid time range")
        if record["duration_exact"] and (record["end"] is None or record["duration_ms"] is None):
            raise ValueError("Incomplete duration")
    else:
        if record["counter"] is not None and not is_hash(record["counter"]):
            raise ValueError("Invalid counter identity")
        if record["model"] is not None and (
            not isinstance(record["model"], str) or len(record["model"]) > 200
        ):
            raise ValueError("Invalid model")
        if (
            all(record[k] is not None for k in ("input", "cached", "write"))
            and record["cached"] + record["write"] > record["input"]
        ):
            raise ValueError("Invalid cache counts")
    return record


def save(connection, record):
    connection.execute(
        UPSERT,
        (
            record["id"],
            record["kind"],
            record["updated"],
            record.get("counter"),
            json.dumps(record, allow_nan=False),
        ),
    )


class Usage:
    def __init__(self, config, agents=AGENTS):
        self.config = config
        self.agents = agents
        self.path = Path(config["history"]["database"]).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > VERSION:
                raise ValueError("Unsupported history database version")
            db.execute(
                "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, updated REAL NOT NULL, counter TEXT UNIQUE, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, signature TEXT NOT NULL, offset INTEGER NOT NULL, state TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if version < VERSION:
                rows = db.execute("SELECT id, data FROM records").fetchall()
                for key, data in rows:
                    record = upgrade(json.loads(data), agents)
                    db.execute("UPDATE records SET data=? WHERE id=?", (json.dumps(record), key))
                # Parser state holds earlier records; replay files once, merging by identity.
                db.execute("DELETE FROM files")
            db.execute(f"PRAGMA user_version={VERSION}")
            # Settings of agents no longer in the registry are ignored, not fatal.
            self.source_overrides = {
                key: value
                for key, value in self.setting(db, "sources").items()
                if key in agents and isinstance(value, str)
            }
            self.connections = {
                key: value
                for key, value in self.setting(db, "connections").items()
                if key in agents and type(value) is bool
            }
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.generation, self.cached_generation = 0, -1
        self.cached = None
        self.scanning, self.warnings = True, []
        self.source_counts = {}

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def setting(db, key):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        value = json.loads(row[0]) if row else {}
        if not isinstance(value, dict):
            raise ValueError("Invalid settings")
        return value

    def store(self, key, values):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(values)))

    def source_roots(self):
        configured = self.config.get("agents", {})
        return {
            agent.id: Path(
                self.source_overrides.get(agent.id)
                or configured.get(agent.id, {}).get("history")
                or agent.default_history()
            )
            .expanduser()
            .absolute()
            for agent in self.agents.values()
        }

    def sources(self):
        return {
            agent: {
                "path": str(path),
                "override": self.source_overrides.get(agent, ""),
                "exists": path.is_dir(),
                "files": self.source_counts.get(agent),
            }
            for agent, path in self.source_roots().items()
        }

    def set_sources(self, values):
        if not isinstance(values, dict) or set(values) != set(self.agents):
            raise ValueError("Invalid source settings")
        for value in values.values():
            if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid source path")
            if value and (
                not Path(value).expanduser().is_absolute() or not Path(value).expanduser().is_dir()
            ):
                raise ValueError("Source must be an existing directory")
        with self.lock:
            self.store("sources", values)
            self.source_overrides = values.copy()
            self.source_counts = {}
        return self.sources()

    def set_connection(self, agent, enabled):
        if agent not in self.agents or type(enabled) is not bool:
            raise ValueError("Invalid connection")
        with self.lock:
            values = {**self.connections, agent: enabled}
            self.store("connections", values)
            self.connections = values

    def paths(self):
        for agent, root in self.source_roots().items():
            if self.connections.get(agent) is not False:
                for path in self.agents[agent].history_files(root):
                    yield agent, path

    def sync(self):
        warnings = []
        counts = dict.fromkeys(self.agents, 0)
        with self.lock, closing(self.connect()) as db:
            for agent, path in self.paths():
                counts[agent] += 1
                if self.stop.is_set():
                    break
                try:
                    stat = path.stat()
                    signature = json.dumps(
                        [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
                    )
                    prior = db.execute(
                        "SELECT signature, offset, state FROM files WHERE path=?", (str(path),)
                    ).fetchone()
                    if prior and prior[0] == signature:
                        continue
                    offset, state = (prior[1], json.loads(prior[2])) if prior else (0, {})
                    if prior and (
                        stat.st_size <= offset
                        or json.loads(prior[0])[:2] != [stat.st_dev, stat.st_ino]
                    ):
                        offset, state = 0, {}
                    parse = self.agents[agent].parse
                    with db, path.open("rb") as stream:
                        before = db.total_changes
                        stream.seek(offset)
                        while line := stream.readline():
                            if not line.endswith(b"\n"):
                                break
                            offset = stream.tell()
                            try:
                                for record in parse(json.loads(line), state):
                                    save(db, validate(record, self.agents))
                            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                                state["invalid"] = state.get("invalid", 0) + 1
                        changed = db.total_changes != before
                        db.execute(
                            "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                            (str(path), signature, offset, json.dumps(state)),
                        )
                    if changed:
                        self.generation += 1
                except OSError:
                    warnings.append("Some local session files could not be read.")
            invalid = sum(
                json.loads(row[0]).get("invalid", 0)
                for row in db.execute("SELECT state FROM files")
            )
            if invalid:
                warnings.append(f"{invalid} unsupported or invalid native records skipped.")
            self.source_counts = counts
        self.scanning = False
        self.warnings = list(dict.fromkeys(warnings))

    def start(self):
        def watch():
            while not self.stop.is_set():
                try:
                    self.sync()
                except Exception as exc:
                    logging.warning("History scan failed (%s)", type(exc).__name__)
                    self.warnings = ["Local history could not be refreshed."]
                if self.stop.wait(self.config["collection"]["refresh_seconds"]):
                    break

        self.worker = threading.Thread(target=watch, daemon=True)
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=5)

    def records(self):
        with closing(self.connect()) as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM records ORDER BY kind, id")
            ]

    def export(self):
        return {"format": FORMAT, "version": VERSION, "records": self.records()}

    def import_data(self, payload):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"format", "version", "records"}
            or payload["format"] != FORMAT
            or type(payload["version"]) is not int
            or not 1 <= payload["version"] <= VERSION
            or not isinstance(payload["records"], list)
        ):
            raise ValueError("Unsupported history file")
        current = payload["version"] == VERSION
        records = [
            validate(record if current else upgrade(record, self.agents), self.agents)
            for record in payload["records"]
        ]
        with self.lock, closing(self.connect()) as db, db:
            before = db.total_changes
            for record in records:
                save(db, record)
            changed = db.total_changes - before
        self.generation += 1
        return {"imported": changed}

    def snapshot(self):
        generation = self.generation
        if self.cached is None or generation != self.cached_generation:
            records = self.records()
            prompts = {r["id"]: r for r in records if r["kind"] == "prompt"}
            calls = {}
            for record in records:
                if record["kind"] == "call":
                    calls.setdefault(record["turn"], []).append(record)
            rows = []
            for key, source in prompts.items():
                group = calls.get(key, [])
                row = {k: v for k, v in source.items() if k not in ("kind", "turn", "updated")}
                row["models"] = sorted({c["model"] for c in group if c.get("model")})
                for field in ("input", "output", "cached", "write", "reasoning"):
                    row[field] = (
                        sum(c[field] for c in group)
                        if group and all(c[field] is not None for c in group)
                        else None
                    )
                row["tokens"] = (
                    row["input"] + row["output"]
                    if row["input"] is not None and row["output"] is not None
                    else None
                )
                costs = [estimate(c, self.config.get("prices", {})) for c in group]
                row["cost"] = sum(costs) if costs and all(c is not None for c in costs) else None
                rows.append(row)
            skipped = sum(len(group) for key, group in calls.items() if key not in prompts)
            self.cached = dict(
                rows=sorted(rows, key=lambda r: r["start"], reverse=True), skipped=skipped
            )
            self.cached_generation = generation
        warnings = self.warnings.copy()
        if self.scanning:
            warnings.append("Reading local history…")
        if self.cached["skipped"]:
            warnings.append(f"{self.cached['skipped']} model calls have no recorded parent prompt.")
        return dict(
            rows=self.cached["rows"],
            warnings=warnings,
            source_counts=self.source_counts.copy(),
        )
