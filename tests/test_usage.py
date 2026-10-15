import copy
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from usage.history import Usage
from usage.readers import estimate, project_path


def event(kind, second, **payload):
    return dict(
        type="event_msg",
        timestamp=f"2026-10-14T10:00:{second:02d}Z",
        payload=dict(type=kind, **payload),
    )


def usage(inp, out, cached=0):
    return dict(
        input_tokens=inp,
        output_tokens=out,
        cached_input_tokens=cached,
        cache_write_input_tokens=0,
        reasoning_output_tokens=0,
        total_tokens=inp + out,
    )


def snapshot(second, total, last=None):
    return event(
        "token_count", second, info=dict(total_token_usage=total, last_token_usage=last or total)
    )


def session(sid="session", fork=None):
    return dict(
        type="session_meta",
        payload=dict(id=sid, forked_from_id=fork, timestamp="2026-10-14T10:00:00Z"),
    )


def context(model="model"):
    return dict(type="turn_context", timestamp="2026-10-14T10:00:01Z", payload=dict(model=model))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = dict(
            history=dict(database=str(self.root / "history.sqlite3")),
            transcripts=dict(
                codex=str(self.root / "codex"), claude=str(self.root / "claude"), show_question=True
            ),
            collection=dict(refresh_seconds=1),
            prices={"model": dict(input=10, cached=1, write=12.5, output=50)},
        )
        self.env = patch.dict(os.environ, {"CODEX_HOME": "", "CLAUDE_CONFIG_DIR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Usage(self.config)

    def write(self, events, name="session.jsonl", agent="codex", append=False):
        path = self.root / agent / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w", encoding="utf-8") as stream:
            for record in events:
                stream.write(json.dumps(record) + "\n")
        return path

    def basic(self):
        return [
            session(),
            context(),
            event("task_started", 1, turn_id="one"),
            event("user_message", 2, message="First question"),
            snapshot(3, usage(1000, 100, 800)),
            event("task_complete", 4, turn_id="one", duration_ms=3000),
        ]

    def test_legacy_deltas_duplicates_completion_and_cache_price(self):
        records = self.basic()
        records[4:4] = [snapshot(3, usage(1000, 100, 800))]
        records += [
            event("task_started", 5, turn_id="two"),
            event(
                "item_completed",
                6,
                item=dict(type="UserMessage", content=[dict(type="text", text="Second question")]),
            ),
            snapshot(7, usage(1200, 130, 900), usage(200, 30, 100)),
        ]
        self.write(records)
        self.store.sync()
        rows = self.store.snapshot()["rows"]
        self.assertEqual([r["tokens"] for r in rows], [230, 1100])
        self.assertEqual([r["call_count"] for r in rows], [1, 1])
        self.assertEqual([r["title"] for r in rows], ["Second question", "First question"])
        self.assertFalse(rows[0]["duration_exact"])
        self.assertEqual(rows[0]["status"], "observed")
        self.assertEqual(rows[1]["duration_ms"], 3000)
        self.assertAlmostEqual(rows[1]["cost"], 0.0078)
        self.assertIsNone(estimate(dict(input=1, output=1, cached=0, write=0), {}))

    def test_response_ids_subagents_and_following_snapshots_count_once(self):
        native = dict(
            type="token_usage_record",
            timestamp="2026-10-14T10:00:03Z",
            payload=dict(
                response_id="response",
                thread_id="session",
                turn_token_usage=usage(1000, 100, 800),
                turn_id="one",
                root_turn_id="one",
                usage=usage(1000, 100, 800),
                thread_token_usage=usage(1000, 100, 800),
            ),
        )
        records = self.basic()
        records.insert(5, native)
        records.insert(5, native)
        self.write(records)
        child = copy.deepcopy(native)
        child["payload"].update(
            response_id="child-response",
            turn_id="child",
            usage=usage(50, 10),
            thread_token_usage=usage(50, 10),
        )
        self.write(
            [
                session("child"),
                context(),
                event("task_started", 2, turn_id="child", root_turn_id="one"),
                child,
                snapshot(3, usage(50, 10)),
                event("task_complete", 4, turn_id="child"),
            ],
            "child.jsonl",
        )
        self.store.sync()
        rows = self.store.snapshot()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], 1160)
        self.assertEqual(rows[0]["call_count"], 2)

    def test_fork_prefix_is_baseline_not_new_usage(self):
        meta = session("child", "parent")
        meta["payload"]["timestamp"] = "2026-10-14T10:00:05Z"
        self.write(
            [
                meta,
                context(),
                event("task_started", 1, turn_id="old"),
                snapshot(2, usage(900, 90)),
                event("task_complete", 3, turn_id="old"),
                event("task_started", 6, turn_id="new"),
                snapshot(7, usage(1000, 100), usage(100, 10)),
            ]
        )
        self.store.sync()
        rows = self.store.snapshot()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], 110)

    def test_modern_response_stream_does_not_mix_different_cumulative_counters(self):
        records = self.basic()[:4]
        native = dict(
            type="token_usage_record",
            timestamp="2026-10-14T10:00:03Z",
            payload=dict(
                response_id="response",
                thread_id="session",
                turn_id="one",
                root_turn_id="one",
                usage=usage(1000, 100),
                turn_token_usage=usage(1000, 100),
                thread_token_usage=usage(5000, 500),
            ),
        )
        records += [native, snapshot(4, usage(1000, 100)), snapshot(5, usage(1000, 100))]
        self.write(records)
        self.store.sync()
        row = self.store.snapshot()["rows"][0]
        self.assertEqual((row["tokens"], row["call_count"]), (1100, 1))

    def test_preliminary_counter_export_does_not_duplicate_later_response(self):
        self.write(self.basic()[:-1])
        self.store.sync()
        earlier = self.store.export()
        native = dict(
            type="token_usage_record",
            timestamp="2026-10-14T10:00:04Z",
            payload=dict(
                response_id="response",
                thread_id="session",
                turn_id="one",
                root_turn_id="one",
                usage=usage(1000, 100, 800),
                turn_token_usage=usage(1000, 100, 800),
                thread_token_usage=usage(1000, 100, 800),
            ),
        )
        self.write([native], append=True)
        self.store.sync()
        self.assertEqual(self.store.import_data(earlier)["imported"], 0)
        self.assertEqual(self.store.snapshot()["rows"][0]["call_count"], 1)

    def test_claude_stream_duplicates_tool_results_and_late_parent(self):
        def entry(kind, second, uid, parent=None, **extra):
            return dict(
                type=kind,
                timestamp=f"2026-10-14T10:00:{second:02d}Z",
                uuid=uid,
                parentUuid=parent,
                sessionId="claude",
                **extra,
            )

        def answer(second, output):
            return entry(
                "assistant",
                second,
                "a",
                "u1",
                message=dict(
                    id="message-1",
                    model="model",
                    stop_reason="end_turn",
                    usage=dict(
                        input_tokens=10,
                        cache_read_input_tokens=200,
                        cache_creation_input_tokens=50,
                        output_tokens=output,
                    ),
                ),
            )

        self.write(
            [
                entry("user", 1, "u1", message=dict(content="First")),
                answer(2, 1),
                entry(
                    "user",
                    3,
                    "tool-result",
                    "a",
                    message=dict(content=[dict(type="tool_result", content="SECRET TOOL RESULT")]),
                ),
                entry("user", 4, "u2", "tool-result", message=dict(content="Second")),
                answer(5, 20),
            ],
            agent="claude",
        )
        self.store.sync()
        rows = self.store.snapshot()["rows"]
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["tokens"])
        self.assertEqual(rows[1]["tokens"], 280)
        self.assertEqual(rows[1]["call_count"], 1)
        self.assertFalse(rows[1]["duration_exact"])
        self.assertNotIn("SECRET TOOL RESULT", json.dumps(self.store.export()))

    def test_incremental_partial_line_restart_truncation_and_source_deletion(self):
        path = self.write(self.basic()[:-1])
        tail = json.dumps(self.basic()[-1])
        with path.open("a") as stream:
            stream.write(tail[:20])
        self.store.sync()
        self.assertEqual(self.store.snapshot()["rows"][0]["status"], "observed")
        with path.open("a") as stream:
            stream.write(tail[20:] + "\n")
        restarted = Usage(self.config)
        restarted.sync()
        self.assertEqual(restarted.snapshot()["rows"][0]["status"], "completed")
        self.write(self.basic())
        restarted.sync()
        self.assertEqual(restarted.snapshot()["rows"][0]["tokens"], 1100)
        path.unlink()
        restarted.sync()
        self.assertEqual(restarted.snapshot()["rows"][0]["tokens"], 1100)

    def test_export_import_atomic_idempotent_and_older_data_cannot_replace(self):
        self.write(self.basic())
        self.store.sync()
        payload = self.store.export()
        config = copy.deepcopy(self.config)
        config["history"]["database"] = str(self.root / "imported.sqlite3")
        target = Usage(config)
        self.assertEqual(target.import_data(payload)["imported"], 2)
        self.assertEqual(target.import_data(payload)["imported"], 0)
        self.assertEqual(target.snapshot()["rows"], self.store.snapshot()["rows"])
        invalid = copy.deepcopy(payload)
        invalid["records"][0]["updated"] += 1
        invalid["records"][-1]["extra"] = "SECRET"
        with self.assertRaises(ValueError):
            target.import_data(invalid)
        self.assertEqual(target.export(), payload)
        old = copy.deepcopy(payload)
        old["records"][0]["updated"] -= 1
        self.assertEqual(target.import_data(old)["imported"], 0)
        invalid = copy.deepcopy(payload)
        invalid["records"][0]["updated"] = float("nan")
        with self.assertRaises(ValueError):
            target.import_data(invalid)
        with closing(target.connect()) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_private_mode_and_malformed_records(self):
        self.config["transcripts"]["show_question"] = False
        path = self.write(self.basic())
        with path.open("a") as stream:
            stream.write("{bad json}\n")
        self.store.sync()
        self.assertIsNone(self.store.snapshot()["rows"][0]["title"])
        self.assertNotIn("First question", json.dumps(self.store.export()))
        self.assertIn("1 unsupported", self.store.snapshot()["warnings"][0])

    def test_source_switch_persists_keeps_history_and_restores_discovery(self):
        original = self.write(self.basic())
        self.store.sync()
        records = self.basic()
        records[0] = session("another-session")
        for record in records:
            if record.get("payload", {}).get("turn_id") == "one":
                record["payload"]["turn_id"] = "another-turn"
        other = self.write(records, agent="other")
        values = {"codex": str(other.parent), "claude": ""}
        with patch.dict(os.environ, {"CODEX_HOME": str(original.parent)}):
            result = self.store.set_sources(values)
            self.assertEqual(result["codex"]["path"], str(other.parent))
            self.assertEqual(list(self.store.paths()), [("Codex", other)])
            restarted = Usage(self.config)
            restarted.sync()
            self.assertEqual(len(restarted.snapshot()["rows"]), 2)
            self.assertEqual(restarted.sources()["codex"]["override"], str(other.parent))
            restarted.set_sources({"codex": "", "claude": ""})
            self.assertEqual(list(restarted.paths()), [("Codex", original)])
            self.assertEqual(len(restarted.snapshot()["rows"]), 2)

    def test_invalid_sources_are_rejected_without_changing_settings(self):
        before = self.store.sources()
        for values in [
            {},
            {"codex": str(self.root), "claude": str(self.root / "missing")},
            {"codex": str(self.root), "claude": "relative"},
            {"codex": "invalid\x00path", "claude": ""},
            {"codex": 123, "claude": ""},
            {"codex": "", "claude": "", "command": "anything"},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.store.set_sources(values)
            self.assertEqual(self.store.sources(), before)

    def test_project_metadata_tracks_each_turn_and_portable_paths(self):
        records = self.basic()
        records[0]["payload"]["cwd"] = "/work/first"
        records.insert(
            3,
            dict(
                type="turn_context",
                timestamp="2026-10-14T10:00:02Z",
                payload=dict(cwd="/work/second/./src/..", model="model"),
            ),
        )
        records += [
            event("task_started", 5, turn_id="two"),
            event("user_message", 6, message="Next"),
        ]
        self.write(records)
        self.write(
            [
                dict(
                    type="user",
                    sessionId="claude-project",
                    uuid="user-id",
                    cwd="C:\\Work\\App",
                    timestamp="2026-10-14T10:00:10Z",
                    message=dict(content="Question"),
                )
            ],
            agent="claude",
        )
        self.store.sync()
        rows = self.store.snapshot()["rows"]
        self.assertEqual(
            [row["project"] for row in rows], ["c:/work/app", "/work/second", "/work/second"]
        )
        self.assertEqual(self.store.snapshot()["source_counts"], {"codex": 1, "claude": 1})
        for value in (None, 123, "relative/path", "C:relative", "/bad\x00path"):
            self.assertIsNone(project_path(value))
        self.assertEqual(project_path("\\\\SERVER\\Share\\Repo"), "//server/share/repo")

    def test_project_backfill_preserves_archive_and_legacy_imports(self):
        records = self.basic()
        records[0]["payload"]["cwd"] = "/work/project"
        self.write(records)
        self.store.sync()
        known = self.store.export()
        legacy = copy.deepcopy(known)
        legacy["version"] = 1
        for record in legacy["records"]:
            record.pop("project", None)
        with closing(self.store.connect()) as db, db:
            db.execute("UPDATE records SET data=json_remove(data, '$.project') WHERE kind='prompt'")
            db.execute("PRAGMA user_version=1")
        migrated = Usage(self.config)
        self.assertEqual(migrated.snapshot()["rows"][0]["tokens"], 1100)
        self.assertIsNone(migrated.snapshot()["rows"][0]["project"])
        migrated.sync()
        self.assertEqual(migrated.export(), known)
        migrated.import_data(legacy)
        self.assertEqual(migrated.export(), known)
        config = copy.deepcopy(self.config)
        config["history"]["database"] = str(self.root / "legacy.sqlite3")
        imported = Usage(config)
        imported.import_data(legacy)
        self.assertIsNone(imported.snapshot()["rows"][0]["project"])
        imported.import_data(known)
        self.assertEqual(imported.export(), known)
        newer = copy.deepcopy(legacy)
        for record in newer["records"]:
            if record["kind"] == "prompt":
                record["updated"] += 10
        with closing(imported.connect()) as db, db:
            db.execute("DELETE FROM records")
        imported.import_data(newer)
        imported.sync()
        self.assertEqual(imported.snapshot()["rows"][0]["project"], "/work/project")
        self.assertEqual(imported.snapshot()["rows"][0]["tokens"], 1100)
