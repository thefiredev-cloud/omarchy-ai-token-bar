"""Tests for plugins/meshvault.ai-tokens/ai_tokens.py.

Every test builds its own fixtures in a temporary directory and runs the
script as a subprocess, so no real home files are read.
"""

import fcntl
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "plugins", "meshvault.ai-tokens", "ai_tokens.py")
TZ = "America/Los_Angeles"
SECRET = "SECRET-PROMPT-TEXT-do-not-store"
_OLD_TZ = None


def setUpModule():
    global _OLD_TZ
    _OLD_TZ = os.environ.get("TZ")
    os.environ["TZ"] = TZ
    time.tzset()


def tearDownModule():
    if _OLD_TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _OLD_TZ
    time.tzset()


def local_epoch(y, mo, d, h=12, mi=0, s=0):
    """Epoch seconds for a wall-clock time in the test time zone."""
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


def iso_utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


NOW = local_epoch(2026, 10, 2, 12)


def claude_line(mid, rid, epoch, inp, out, cw=0, cr=0, model="claude-opus-5-5", block=0):
    return json.dumps({
        "type": "assistant",
        "timestamp": iso_utc(epoch),
        "requestId": rid,
        "sessionId": "s1",
        "message": {
            "id": mid,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": f"{SECRET} block {block}"}],
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr},
        },
    }) + "\n"


def codex_count(epoch, inp, cached, out, null_info=False):
    info = None if null_info else {"total_token_usage": {
        "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
        "reasoning_output_tokens": 1, "total_tokens": inp + out}}
    return json.dumps({"timestamp": iso_utc(epoch), "type": "event_msg",
                       "payload": {"type": "token_count", "info": info}}) + "\n"


def codex_context(model):
    return json.dumps({"timestamp": iso_utc(NOW), "type": "turn_context",
                       "payload": {"model": model, "cwd": SECRET}}) + "\n"


def grok_turn(number, epoch, inp, cached, out, cw=0, calls=1, model_usage=True):
    fields = {"inputTokens": inp, "outputTokens": out, "cachedReadTokens": cached,
              "cacheCreationTokens": cw, "reasoningTokens": 3, "modelCalls": calls}
    turn = dict(fields, turnNumber=number, endedAt=iso_utc(epoch), primaryModelId="grok-4.7-build")
    if model_usage:
        turn["modelUsage"] = {"grok-4.7-build": dict(fields)}
    return turn


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.claude = os.path.join(self.tmp, "claude")
        self.codex = os.path.join(self.tmp, "codex")
        self.grok = os.path.join(self.tmp, "grok")
        self.state = os.path.join(self.tmp, "state")
        self.omp_db = os.path.join(self.tmp, "omp.db")
        self.hermes_db = os.path.join(self.tmp, "hermes.db")
        for path in (self.claude, self.codex, self.grok, os.path.join(self.tmp, "home")):
            os.makedirs(path)
        with sqlite3.connect(self.omp_db) as con:
            con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, provider TEXT, model TEXT, "
                        "timestamp INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
                        "cache_read_tokens INTEGER, cache_write_tokens INTEGER, error_message TEXT)")
        con.close()
        with sqlite3.connect(self.hermes_db) as con:
            con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, billing_provider TEXT, model TEXT, "
                        "system_prompt TEXT, started_at REAL, ended_at REAL, input_tokens INTEGER, "
                        "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
                        "api_call_count INTEGER)")
            con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
        con.close()
        self.env = dict(os.environ)
        self.env.update({
            "HOME": os.path.join(self.tmp, "home"),
            "TZ": TZ,
            "CLAUDE_PROJECTS_DIR": self.claude,
            "CODEX_SESSIONS_DIR": self.codex,
            "GROK_SESSIONS_DIR": self.grok,
            "OMP_STATS_DB": self.omp_db,
            "HERMES_STATE_DB": self.hermes_db,
            "OMP_BIN": os.path.join(self.tmp, "no-such-omp"),
        })
        self.env.pop("MESHVAULT_TOKENS_STATE", None)

    def tearDown(self):
        for root, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), 0o700)
                except OSError:
                    pass
        self._tmp.cleanup()

    def invoke(self, *args, now=NOW, state=None, sync=False):
        cmd = [sys.executable, SCRIPT, "--state-dir", state or self.state]
        if now is not None:
            cmd += ["--now", repr(now)]
        if not sync:
            cmd.append("--no-omp-sync")
        return subprocess.run(cmd + list(args), env=self.env, capture_output=True, text=True, timeout=60)

    def run_ok(self, *args, **kw):
        proc = self.invoke(*args, **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1, "stdout must be exactly one line")
        return json.loads(proc.stdout)

    def write(self, path, text, mode="w"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as fh:
            fh.write(text)

    @staticmethod
    def src(doc, provider, source, period="all"):
        return doc["providers"][provider]["sources"][source][period]

    def errors(self, doc):
        return {e["source"]: e["message"] for e in doc["errors"]}


class ClaudeTests(Base):
    def test_duplicate_lines_once_across_files_and_runs(self):
        a = os.path.join(self.claude, "proj", "a.jsonl")
        b = os.path.join(self.claude, "proj", "sub", "agent-b.jsonl")
        t = NOW - 3600
        lines = [claude_line("msg_1", "req_1", t, 10, 5, 100, 1000, block=i) for i in range(3)]
        lines.append(claude_line("msg_2", "req_2", t, 20, 7, 0, 50))
        lines.append(json.dumps({"type": "user", "message": {"content": SECRET}}) + "\n")
        self.write(a, "".join(lines))
        # Resumed session copies msg_1 into a second file.
        self.write(b, claude_line("msg_1", "req_1", t, 10, 5, 100, 1000))
        doc = self.run_ok()
        got = self.src(doc, "anthropic", "claude_code")
        self.assertEqual(got, {"input": 30, "output": 12, "cache_write": 100, "cache_read": 1050,
                               "total": 142, "requests": 2})
        self.assertEqual(self.src(doc, "anthropic", "claude_code", "today")["total"], 142)
        self.assertEqual(self.errors(doc), {})
        # Second run, nothing new.
        self.assertEqual(self.src(self.run_ok(), "anthropic", "claude_code"), got)
        # Later run: a copied old message plus one new message.
        self.write(b, claude_line("msg_2", "req_2", t, 20, 7, 0, 50) + claude_line("msg_3", "req_3", t, 1, 2), "a")
        got = self.src(self.run_ok(), "anthropic", "claude_code")
        self.assertEqual((got["input"], got["output"], got["requests"]), (31, 14, 3))

    def test_synthetic_and_zero_usage_skipped(self):
        path = os.path.join(self.claude, "p", "x.jsonl")
        self.write(path, claude_line("m1", "r1", NOW, 5, 5, model="<synthetic>")
                   + claude_line("m2", "r2", NOW, 0, 0) + claude_line("m3", "r3", NOW, 1, 1))
        got = self.src(self.run_ok(), "anthropic", "claude_code")
        self.assertEqual((got["total"], got["requests"]), (2, 1))

    def test_incremental_append_and_partial_last_line(self):
        path = os.path.join(self.claude, "p", "s.jsonl")
        second = claude_line("m2", "r2", NOW, 200, 20)
        self.write(path, claude_line("m1", "r1", NOW, 100, 10) + second[:40])
        got = self.src(self.run_ok(), "anthropic", "claude_code")
        self.assertEqual((got["input"], got["requests"]), (100, 1))
        self.write(path, second[40:], "a")
        got = self.src(self.run_ok(), "anthropic", "claude_code")
        self.assertEqual((got["input"], got["output"], got["requests"]), (300, 30, 2))
        self.write(path, claude_line("m3", "r3", NOW, 1, 1), "a")
        got = self.src(self.run_ok(), "anthropic", "claude_code")
        self.assertEqual((got["input"], got["requests"]), (301, 3))
        self.assertEqual(self.errors(self.run_ok()), {})

    def test_budgeted_runs_finish_the_backfill(self):
        for n in range(4):
            self.write(os.path.join(self.claude, "p", f"f{n}.jsonl"),
                       claude_line(f"m{n}", f"r{n}", NOW, 10 + n, 1))
        self.write(os.path.join(self.codex, "rollout-a.jsonl"), codex_count(NOW, 50, 0, 5))
        reference = self.src(self.run_ok("--backfill", state=os.path.join(self.tmp, "ref")),
                             "anthropic", "claude_code")
        runs = 0
        while True:
            runs += 1
            doc = self.run_ok("--budget", "0.000001")
            if doc["backfill"]["complete"]:
                break
            self.assertGreater(doc["backfill"]["pending_files"], 0)
            self.assertLess(runs, 10)
        self.assertGreater(runs, 1)
        self.assertEqual(self.src(doc, "anthropic", "claude_code"), reference)
        self.assertEqual(self.src(doc, "codex", "codex_cli")["input"], 50)


class CodexTests(Base):
    def test_cumulative_delta_reset_and_null_info(self):
        path = os.path.join(self.codex, "2026", "10", "02", "rollout-x.jsonl")
        self.write(path, "".join([
            codex_context("gpt-6.1-sol"),
            codex_count(NOW, 0, 0, 0, null_info=True),
            codex_count(NOW, 100, 20, 10),
            codex_count(NOW, 100, 20, 10),  # repeat, no growth
            codex_count(NOW, 250, 50, 30),
            codex_count(NOW, 40, 0, 5),  # session counter reset
        ]))
        self.write(os.path.join(self.codex, "notes.jsonl"), codex_count(NOW, 999, 0, 999))
        doc = self.run_ok()
        got = self.src(doc, "codex", "codex_cli")
        self.assertEqual(got, {"input": 240, "output": 35, "cache_write": 0, "cache_read": 50,
                               "total": 275, "requests": 3})
        self.assertEqual([m["model"] for m in doc["providers"]["codex"]["models"]], ["gpt-6.1-sol"])
        # Appending continues from the stored baseline (40, 0, 5).
        self.write(path, codex_count(NOW, 60, 0, 9), "a")
        got = self.src(self.run_ok(), "codex", "codex_cli")
        self.assertEqual((got["input"], got["output"], got["requests"]), (260, 39, 4))

    def test_model_defaults_to_codex(self):
        self.write(os.path.join(self.codex, "rollout-y.jsonl"), codex_count(NOW, 10, 0, 1))
        doc = self.run_ok()
        self.assertEqual(doc["providers"]["codex"]["models"][0]["model"], "codex")


class GrokTests(Base):
    def test_rewritten_usage_counts_each_turn_once(self):
        path = os.path.join(self.grok, "proj", "sess1", "usage.json")
        t1 = grok_turn(1, NOW - 600, 1000, 600, 30, cw=5, calls=2)
        self.write(path, json.dumps({"sessionId": "sess1", "turns": [t1], "session": {"inputTokens": 1000}}))
        got = self.src(self.run_ok(), "grok", "grok_cli")
        self.assertEqual(got, {"input": 400, "output": 30, "cache_write": 5, "cache_read": 600,
                               "total": 435, "requests": 2})
        t2 = grok_turn(2, NOW - 60, 500, 100, 20, model_usage=False)
        self.write(path, json.dumps({"sessionId": "sess1", "turns": [t1, t2]}))
        doc = self.run_ok()
        got = self.src(doc, "grok", "grok_cli")
        self.assertEqual((got["input"], got["output"], got["cache_read"], got["requests"]), (800, 50, 700, 3))
        self.assertEqual([m["model"] for m in doc["providers"]["grok"]["models"]], ["grok-4.7"])
        self.assertEqual(self.src(self.run_ok(), "grok", "grok_cli"), got)


class HermesTests(Base):
    def put(self, sid, provider, started, ended, inp, out, cr, cw, model="claude-opus-5-5", calls=None):
        with sqlite3.connect(self.hermes_db) as con:
            con.execute("INSERT OR REPLACE INTO sessions (id, billing_provider, model, system_prompt, started_at, "
                        "ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                        "api_call_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, provider, model, SECRET, started, ended, inp, out, cr, cw, calls))
        con.close()

    def test_first_seen_then_delta_today(self):
        three_days_ago = NOW - 3 * 86400
        self.put("a", "anthropic", three_days_ago - 100, three_days_ago, 100, 50, 1000, 10, calls=4)
        self.put("b", "openai-codex", NOW - 86400, None, 30, 3, 0, 0, model=None)
        self.put("c", "claude-subscription-directsdk-experimental", NOW, NOW, 1, 1, 0, 0)
        for ignored in ("custom", "custom:cluster", "openrouter", "nous", None):
            self.put(f"x-{ignored}", ignored, NOW, NOW, 5000, 5000, 0, 0)
        doc = self.run_ok()
        anth = doc["providers"]["anthropic"]["sources"]["hermes"]
        self.assertEqual(anth["all"], {"input": 101, "output": 51, "cache_write": 10, "cache_read": 1000,
                                       "total": 162, "requests": 5})
        self.assertEqual(anth["today"]["total"], 2)
        self.assertEqual(anth["7d"]["total"], 162)
        codex = self.src(doc, "codex", "hermes")
        self.assertEqual((codex["input"], codex["requests"]), (30, 1))
        self.assertEqual(self.src(doc, "codex", "hermes", "today")["total"], 0)
        self.assertEqual(doc["providers"]["codex"]["models"][0]["model"], "?")
        self.assertEqual(doc["providers"]["grok"]["periods"]["all"]["total"], 0)

        # Session a grows input, its output counter drops: only growth counts, today.
        self.put("a", "anthropic", three_days_ago - 100, three_days_ago, 110, 40, 1000, 10, calls=6)
        doc = self.run_ok()
        today = self.src(doc, "anthropic", "hermes", "today")
        self.assertEqual((today["input"], today["output"], today["requests"]), (11, 1, 2))
        self.assertEqual(self.src(doc, "anthropic", "hermes")["output"], 51)
        # The dropped counter is the new baseline: growing back from 40 counts.
        self.put("a", "anthropic", three_days_ago - 100, three_days_ago, 110, 43, 1000, 10)
        today = self.src(self.run_ok(), "anthropic", "hermes", "today")
        self.assertEqual((today["output"], today["requests"]), (4, 3))
        # No change, no new request.
        self.assertEqual(self.src(self.run_ok(), "anthropic", "hermes", "today")["requests"], 3)


class OmpTests(Base):
    def put(self, rows):
        with sqlite3.connect(self.omp_db) as con:
            con.executemany("INSERT INTO messages (provider, model, timestamp, input_tokens, output_tokens, "
                            "cache_read_tokens, cache_write_tokens) VALUES (?,?,?,?,?,?,?)", rows)
        con.close()

    def test_provider_mapping_and_idempotent_recompute(self):
        ms = int(NOW * 1000)
        self.put([
            ("anthropic", "claude-opus-5-5", ms, 10, 20, 300, 40),
            ("anthropic", "claude-opus-5-5", ms - 86400000 * 2, 1, 2, 3, 4),
            ("openai-codex", "gpt-6.1-sol", ms, 100, 10, 50, 0),
            ("xai-oauth", "grok-4.7", ms, 7, 1, 0, 0),
            ("xai", "grok-4.7", ms, 3, 1, 0, 0),
            ("openrouter", "x/y", ms, 9999, 9999, 0, 0),
            ("cluster", "glm", ms, 9999, 9999, 0, 0),
        ])
        first = self.run_ok()
        anth = self.src(first, "anthropic", "omp")
        self.assertEqual(anth, {"input": 11, "output": 22, "cache_write": 44, "cache_read": 303,
                                "total": 77, "requests": 2})
        self.assertEqual(self.src(first, "anthropic", "omp", "today")["total"], 70)
        self.assertEqual(self.src(first, "codex", "omp")["total"], 110)
        grok = self.src(first, "grok", "omp")
        self.assertEqual((grok["input"], grok["output"], grok["requests"]), (10, 2, 2))
        self.assertAlmostEqual(first["providers"]["grok"]["last_event_at"], ms / 1000.0)
        second = self.run_ok()
        self.assertEqual(first["providers"], second["providers"])
        # A row removed from the database disappears from the slice.
        with sqlite3.connect(self.omp_db) as con:
            con.execute("DELETE FROM messages WHERE provider = 'xai'")
        con.close()
        self.assertEqual(self.src(self.run_ok(), "grok", "omp")["requests"], 1)

    def test_sync_runs_at_most_every_300_seconds(self):
        marker = os.path.join(self.tmp, "sync-calls")
        fake = os.path.join(self.tmp, "fake-omp")
        self.write(fake, f"#!/bin/sh\necho \"$1 $2\" >> {marker}\n")
        os.chmod(fake, 0o700)
        self.env["OMP_BIN"] = fake
        self.run_ok(sync=True)
        self.run_ok(now=NOW + 60, sync=True)
        self.run_ok(now=NOW + 60, sync=False)
        doc = self.run_ok(now=NOW + 310, sync=True)
        with open(marker) as fh:
            self.assertEqual(fh.read().splitlines(), ["stats --summary", "stats --summary"])
        self.assertNotIn("omp", self.errors(doc))

    def test_missing_omp_binary_is_an_error_not_a_crash(self):
        doc = self.run_ok(sync=True)
        self.assertIn("omp", self.errors(doc))


class PeriodTests(Base):
    def test_local_midnight_and_7d_30d_edges(self):
        event = local_epoch(2026, 10, 2, 23, 59, 30)
        self.write(os.path.join(self.claude, "p", "a.jsonl"), claude_line("m", "r", event, 100, 0))

        def periods(now):
            doc = self.run_ok(now=now)
            totals = {p: self.src(doc, "anthropic", "claude_code", p)["total"] for p in ("today", "7d", "30d", "all")}
            return doc["day"], totals

        before = local_epoch(2026, 10, 2, 23, 59, 50)
        after = local_epoch(2026, 10, 3, 0, 0, 10)
        self.assertEqual(periods(before), ("2026-10-02", {"today": 100, "7d": 100, "30d": 100, "all": 100}))
        self.assertEqual(periods(after), ("2026-10-03", {"today": 0, "7d": 100, "30d": 100, "all": 100}))
        self.assertEqual(periods(local_epoch(2026, 10, 8, 23, 59, 59))[1]["7d"], 100)
        self.assertEqual(periods(local_epoch(2026, 10, 9, 0, 0, 1))[1], {"today": 0, "7d": 0, "30d": 100, "all": 100})
        self.assertEqual(periods(local_epoch(2026, 10, 31, 23, 0))[1]["30d"], 100)
        self.assertEqual(periods(local_epoch(2026, 11, 1, 0, 30))[1], {"today": 0, "7d": 0, "30d": 0, "all": 100})
        # The event is 06:59:30 UTC on 10-03, but it belongs to the local day 10-02.
        self.assertTrue(iso_utc(event).startswith("2026-10-03T06:59:30"))


class ContractTests(Base):
    def test_shape_with_no_data(self):
        doc = self.run_ok()
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["generated_at"], NOW)
        self.assertEqual(doc["day"], "2026-10-02")
        self.assertEqual(doc["backfill"], {"complete": True, "pending_files": 0})
        expected = {"anthropic": ("Anthropic", "CLD", {"claude_code", "omp", "hermes"}),
                    "codex": ("Codex", "CDX", {"codex_cli", "omp", "hermes"}),
                    "grok": ("Grok", "GRK", {"grok_cli", "omp", "hermes"})}
        self.assertEqual(set(doc["providers"]), set(expected))
        zero = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "total": 0, "requests": 0}
        for name, (label, short, sources) in expected.items():
            prov = doc["providers"][name]
            self.assertEqual((prov["label"], prov["short"]), (label, short))
            self.assertEqual(set(prov["sources"]), sources)
            self.assertEqual(prov["periods"], {p: zero for p in ("today", "7d", "30d", "all")})
            for source in sources:
                self.assertEqual(prov["sources"][source], {p: zero for p in ("today", "7d", "30d", "all")})
            self.assertEqual(prov["models"], [])
            self.assertIsNone(prov["last_event_at"])
        self.assertEqual(doc["errors"], [])

    def test_periods_sum_sources_and_models_top_8(self):
        lines = [claude_line(f"m{n}", f"r{n}", NOW, 10 * (n + 1), 0, model=f"model-{n}") for n in range(10)]
        self.write(os.path.join(self.claude, "p", "a.jsonl"), "".join(lines))
        with sqlite3.connect(self.omp_db) as con:
            con.execute("INSERT INTO messages (provider, model, timestamp, input_tokens, output_tokens, "
                        "cache_read_tokens, cache_write_tokens) VALUES ('anthropic','model-0',?,5,0,0,0)",
                        (int(NOW * 1000),))
        con.close()
        doc = self.run_ok()
        prov = doc["providers"]["anthropic"]
        self.assertEqual(prov["periods"]["all"]["total"], 555)
        models = prov["models"]
        self.assertEqual(len(models), 8)
        self.assertEqual(models[0], {"model": "model-9", "today": 100, "7d": 100, "30d": 100, "all": 100})
        self.assertNotIn("model-0", [m["model"] for m in models])

    def test_source_failure_isolation(self):
        with open(self.omp_db, "wb") as fh:
            fh.write(b"this is not a database" * 100)
        good = os.path.join(self.claude, "p", "good.jsonl")
        bad = os.path.join(self.claude, "p", "bad.jsonl")
        self.write(good, claude_line("m1", "r1", NOW, 10, 1) + "{not json \"usage\" assistant\n")
        self.write(bad, claude_line("m2", "r2", NOW, 20, 2))
        os.chmod(bad, 0)
        os.rmdir(self.grok)
        with sqlite3.connect(self.hermes_db) as con:
            con.execute("INSERT INTO sessions (id, billing_provider, model, started_at, ended_at, input_tokens, "
                        "output_tokens, cache_read_tokens, cache_write_tokens) VALUES ('h','xai','grok-4.7',?,?,7,1,0,0)",
                        (NOW, NOW))
        con.close()
        doc = self.run_ok()
        errors = self.errors(doc)
        self.assertEqual(set(errors), {"omp", "claude_code", "grok_cli"})
        self.assertIn("1 malformed", errors["claude_code"])
        self.assertIn("1 unreadable", errors["claude_code"])
        self.assertEqual(self.src(doc, "anthropic", "claude_code")["input"], 10)
        self.assertEqual(self.src(doc, "grok", "hermes")["input"], 7)
        self.assertNotIn(self.tmp, json.dumps(doc["errors"]))

    def test_ledger_private_and_atomic(self):
        self.write(os.path.join(self.claude, "p", "a.jsonl"), claude_line("m1", "r1", NOW, 10, 1))
        self.run_ok()
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        ledger_path = os.path.join(self.state, "ai-ledger.json")
        self.assertEqual(stat.S_IMODE(os.stat(ledger_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "ai-claude-seen.bin")).st_mode), 0o600)
        inode = os.stat(ledger_path).st_ino
        self.run_ok()
        self.assertNotEqual(os.stat(ledger_path).st_ino, inode, "ledger must be replaced, not rewritten in place")
        self.assertEqual(sorted(n for n in os.listdir(self.state) if n.endswith(".tmp")), [])
        with open(ledger_path) as fh:
            ledger = json.load(fh)
        self.assertEqual(ledger["daily"]["2026-10-02"]["anthropic"]["claude_code"]["claude-opus-5-5"],
                         [10, 1, 0, 0, 1])

    def test_no_content_stored_or_printed(self):
        self.write(os.path.join(self.claude, "p", "a.jsonl"), claude_line("m1", "r1", NOW, 10, 1))
        self.write(os.path.join(self.codex, "rollout-a.jsonl"), codex_context("gpt-x") + codex_count(NOW, 5, 0, 1))
        proc = self.invoke("--backfill")
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        for name in os.listdir(self.state):
            with open(os.path.join(self.state, name), "rb") as fh:
                self.assertNotIn(SECRET.encode(), fh.read())

    def test_held_lock_reports_saved_totals(self):
        self.write(os.path.join(self.claude, "p", "a.jsonl"), claude_line("m1", "r1", NOW, 10, 1))
        self.run_ok()
        with open(os.path.join(self.state, "ai-ledger.lock"), "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            doc = self.run_ok("--budget", "0.3")
        self.assertIn("ledger", self.errors(doc))
        self.assertEqual(self.src(doc, "anthropic", "claude_code")["input"], 10)

    def test_exit_2_for_bad_arguments_and_unwritable_state(self):
        self.assertEqual(self.invoke("--budget", "-1").returncode, 2)
        self.assertEqual(self.invoke("--budget", "soon").returncode, 2)
        blocker = os.path.join(self.tmp, "a-file")
        self.write(blocker, "x")
        proc = self.invoke(state=os.path.join(blocker, "state"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")

    def test_state_dir_from_env(self):
        self.env["MESHVAULT_TOKENS_STATE"] = os.path.join(self.tmp, "envstate")
        cmd = [sys.executable, SCRIPT, "--no-omp-sync", "--now", repr(NOW)]
        proc = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "envstate", "ai-ledger.json")))

    def test_pretty_is_valid_json(self):
        proc = self.invoke("--pretty")
        self.assertEqual(json.loads(proc.stdout)["version"], 1)


if __name__ == "__main__":
    unittest.main()
