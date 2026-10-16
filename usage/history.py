"""Incremental native log ingestion and a portable local usage archive."""

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from .readers import claude, codex, estimate, number, project_path

FORMAT = "agent-usage"
VERSION = 2
MAX_IMPORT = 64 * 1024 * 1024
COMMON = {"id", "kind", "agent", "session", "turn", "updated"}
PROMPT = COMMON | {"start", "end", "title", "status", "duration_ms", "duration_exact", "project"}
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


def load_connections(path):
    """Read monitoring choices without creating a history database."""
    path = Path(path).expanduser().absolute()
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT value FROM settings WHERE key='connections'").fetchone()
    values = json.loads(row[0]) if row else {}
    if not isinstance(values, dict) or any(
        key not in ("codex", "claude") or type(value) is not bool for key, value in values.items()
    ):
        raise ValueError("Invalid connections")
    return values


def validate(record):
    if not isinstance(record, dict) or record.get("kind") not in ("prompt", "call"):
        raise ValueError("Invalid history record")
    fields = PROMPT if record["kind"] == "prompt" else CALL
    if set(record) != fields or record["agent"] not in ("Codex", "Claude Code"):
        raise ValueError("Invalid history fields")
    for key in ("id", "turn"):
        value = record[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("Invalid record identity")
    if not isinstance(record["session"], str) or not 1 <= len(record["session"]) <= 200:
        raise ValueError("Invalid session")
    for key in (
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
    ):
        if key not in record:
            continue
        value = record[key]
        if value is None and key not in ("updated", "start"):
            continue
        if not isinstance(value, (int, float)) or number(value) is None or value > 1e16:
            raise ValueError("Invalid history number")
    if record["kind"] == "prompt":
        if record["project"] is not None and (
            not isinstance(record["project"], str)
            or project_path(record["project"]) != record["project"]
        ):
            raise ValueError("Invalid project directory")
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
        counter = record["counter"]
        if counter is not None and (
            not isinstance(counter, str)
            or len(counter) != 64
            or any(c not in "0123456789abcdef" for c in counter)
        ):
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
    if record["kind"] == "prompt" and record["project"] is None:
        prior = connection.execute(
            "SELECT json_extract(data, '$.project') FROM records WHERE id=?", (record["id"],)
        ).fetchone()
        if prior and prior[0]:
            record = dict(record, project=prior[0])
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
    if record["kind"] == "prompt" and record["project"]:
        # Backfill metadata even when a newer archive already supplied the usage.
        connection.execute(
            "UPDATE records SET data=json_set(data, '$.project', ?) "
            "WHERE id=? AND kind='prompt' AND json_extract(data, '$.project') IS NULL",
            (record["project"], record["id"]),
        )


class Usage:
    def __init__(self, config):
        self.config = config
        self.path = Path(config["history"]["database"]).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, VERSION):
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
            sources = db.execute("SELECT value FROM settings WHERE key='sources'").fetchone()
            self.source_overrides = json.loads(sources[0]) if sources else {}
            if version == 1:
                db.execute(
                    "UPDATE records SET data=json_set(data, '$.project', NULL) WHERE kind='prompt'"
                )
                # Replay available native files once; existing records are merged by identity.
                db.execute("DELETE FROM files")
            db.execute(f"PRAGMA user_version={VERSION}")
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.generation, self.cached_generation = 0, -1
        self.cached = None
        self.scanning, self.warnings = True, []
        self.source_counts = {}
        self.connections = load_connections(self.path)

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def source_roots(self):
        config = self.config["transcripts"]
        claude_home = os.environ.get("CLAUDE_CONFIG_DIR")
        defaults = {
            "codex": os.environ.get("CODEX_HOME") or config["codex"],
            "claude": str(Path(claude_home) / "projects") if claude_home else config["claude"],
        }
        return {
            name: Path(self.source_overrides.get(name) or path).expanduser().absolute()
            for name, path in defaults.items()
        }

    def sources(self):
        return {
            name: {
                "path": str(path),
                "override": self.source_overrides.get(name, ""),
                "exists": path.is_dir(),
                "files": self.source_counts.get(name),
            }
            for name, path in self.source_roots().items()
        }

    def set_sources(self, values):
        if not isinstance(values, dict) or set(values) != {"codex", "claude"}:
            raise ValueError("Invalid source settings")
        for value in values.values():
            if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid source path")
            if value and (
                not Path(value).expanduser().is_absolute() or not Path(value).expanduser().is_dir()
            ):
                raise ValueError("Source must be an existing directory")
        with self.lock:
            with closing(self.connect()) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('sources', ?)", (json.dumps(values),)
                )
            self.source_overrides = values.copy()
            self.source_counts = {}
        return self.sources()

    def set_connection(self, agent, enabled):
        if agent not in ("codex", "claude") or type(enabled) is not bool:
            raise ValueError("Invalid connection")
        with self.lock:
            values = {**self.connections, agent: enabled}
            with closing(self.connect()) as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('connections', ?)",
                    (json.dumps(values),),
                )
            self.connections = values

    def paths(self):
        sources = self.source_roots()
        root = sources["codex"]
        roots = [root / "sessions", root / "archived_sessions"]
        if not any(p.is_dir() for p in roots):
            roots = [root]
        for directory in roots if self.connections.get("codex") is not False else []:
            for path in directory.rglob("*.jsonl"):
                yield "Codex", path
        if self.connections.get("claude") is not False:
            for path in sources["claude"].rglob("*.jsonl"):
                yield "Claude Code", path

    def sync(self):
        warnings = []
        counts = {"codex": 0, "claude": 0}
        with self.lock, closing(self.connect()) as db:
            for agent, path in self.paths():
                counts["codex" if agent == "Codex" else "claude"] += 1
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
                    parser = codex if agent == "Codex" else claude
                    with db, path.open("rb") as stream:
                        before = db.total_changes
                        stream.seek(offset)
                        while line := stream.readline():
                            if not line.endswith(b"\n"):
                                break
                            offset = stream.tell()
                            try:
                                for record in parser(json.loads(line), state):
                                    if (
                                        not self.config["transcripts"]["show_question"]
                                        and record["kind"] == "prompt"
                                    ):
                                        record["title"] = None
                                    validate(record)
                                    save(db, record)
                            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                                state["invalid"] = state.get("invalid", 0) + 1
                        changed = db.total_changes != before
                        if not self.config["transcripts"]["show_question"] and state.get("active"):
                            state["active"]["title"] = None
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
            or payload["version"] not in (1, VERSION)
            or not isinstance(payload["records"], list)
        ):
            raise ValueError("Unsupported history file")
        records = []
        for record in payload["records"]:
            if (
                payload["version"] == 1
                and isinstance(record, dict)
                and record.get("kind") == "prompt"
            ):
                if set(record) != PROMPT - {"project"}:
                    raise ValueError("Invalid legacy record")
                record = dict(record, project=None)
            records.append(validate(record))
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
                if not self.config["transcripts"]["show_question"]:
                    row["title"] = None
                row["call_count"] = len(group)
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
                row["cost_basis"] = "Standard API estimate"
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
            updated_at=time.time(),
            refresh_seconds=self.config["collection"]["refresh_seconds"],
            source_counts=self.source_counts.copy(),
        )
