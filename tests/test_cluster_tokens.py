"""Behavior tests for the cluster token ledger."""

import contextlib
import datetime as dt
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "meshvault.cluster-tokens"))

import cluster_tokens as ct  # noqa: E402


def utc(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def sample(ts, prompt=0, gen=0, reqs=0, up=True, **extra):
    row = {
        "ts": ts, "first": False, "restart": False, "gap": False, "interval_s": 60.0,
        "engine_up": up, "error": None if up else "HTTP503", "served_model": "glm-test",
        "d_prompt_tokens": prompt, "d_generation_tokens": gen, "d_requests_total": reqs,
        "running": 0, "waiting": 0, "kv_usage": 0.0, "decode_tps_per_req": None,
        "max_model_len": 262144,
    }
    row.update(extra)
    return row


class ClusterTokenTests(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.tele = Path(self.tmp.name) / "tele"
        self.tele.mkdir()

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def write(self, name, rows, append=False, partial=""):
        mode = "a" if append else "w"
        with open(self.tele / name, mode, encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
            fh.write(partial)

    def run_once(self, now):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ct.main(["--state-dir", str(self.state), "--telemetry-dir", str(self.tele), "--now", str(now)])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1, "stdout must be exactly one line")
        return json.loads(text)

    def test_adds_each_sample_once_across_runs_and_appends(self):
        now = utc("2026-10-02T20:00:00Z")
        self.write("2026-10-02.jsonl", [
            sample("2026-10-02T19:50:00Z", prompt=1000, gen=100, reqs=2),
            sample("2026-10-02T19:51:00Z", prompt=500, gen=50, reqs=1),
        ])
        first = self.run_once(now)
        self.assertEqual(first["periods"]["today"], {"prompt": 1500, "generation": 150, "total": 1650, "requests": 3})
        again = self.run_once(now)
        self.assertEqual(again["periods"]["today"]["total"], 1650)
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:52:00Z", prompt=10, gen=5, reqs=1)], append=True)
        grown = self.run_once(now)
        self.assertEqual(grown["periods"]["today"], {"prompt": 1510, "generation": 155, "total": 1665, "requests": 4})

    def test_blank_restart_and_down_samples_add_nothing_and_engine_state_follows_last_sample(self):
        now = utc("2026-10-02T20:00:00Z")
        self.write("2026-10-02.jsonl", [
            sample("2026-10-02T19:50:00Z", prompt=200, gen=20, reqs=1),
            sample("2026-10-02T19:51:00Z", prompt=None, gen=None, reqs=None, restart=True, gap=True),
            sample("2026-10-02T19:52:00Z", up=False, prompt=None, gen=None, reqs=None),
            sample("2026-10-02T19:59:30Z", up=True, prompt=7, gen=3, reqs=1, decode_tps_per_req=40.0),
            sample("2026-10-02T19:59:40Z", up=True, prompt=0, gen=0, reqs=0, decode_tps_per_req=60.0),
        ])
        out = self.run_once(now)
        self.assertEqual(out["periods"]["all"]["total"], 230)
        self.assertTrue(out["engine"]["up"])
        self.assertEqual(out["engine"]["decode_tps"], 50.0)
        self.assertEqual(out["engine"]["model"], "glm-test")

    def test_stale_or_down_last_sample_reports_engine_down(self):
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:00:00Z", prompt=1, gen=1, reqs=1)])
        stale = self.run_once(utc("2026-10-02T19:10:00Z"))
        self.assertFalse(stale["engine"]["up"])
        self.assertTrue(stale["engine"]["stale"])
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:09:30Z", up=False, prompt=None, gen=None, reqs=None)], append=True)
        down = self.run_once(utc("2026-10-02T19:10:00Z"))
        self.assertFalse(down["engine"]["up"])
        self.assertFalse(down["engine"]["stale"])
        self.assertEqual(down["engine"]["error"], "HTTP503")

    def test_days_follow_local_midnight_and_periods_have_edges(self):
        # Pacific midnight on 2026-10-02 is 07:00Z.
        self.write("2026-10-02.jsonl", [
            sample("2026-10-02T06:59:00Z", prompt=100, gen=0, reqs=1),
            sample("2026-10-02T07:01:00Z", prompt=10, gen=0, reqs=1),
        ])
        for days_back, tokens in ((6, 1), (7, 2), (29, 4), (30, 8)):
            day = dt.date(2026, 10, 2) - dt.timedelta(days=days_back)
            stamp = dt.datetime.combine(day, dt.time(12, 0)).astimezone().astimezone(dt.timezone.utc)
            self.write("2026-09-xx-%d.jsonl" % days_back, [sample(stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), prompt=tokens, gen=0, reqs=0)])
        out = self.run_once(utc("2026-10-02T19:00:00Z"))
        self.assertEqual(out["periods"]["today"]["prompt"], 10)
        self.assertEqual(out["periods"]["7d"]["prompt"], 10 + 100 + 1)
        self.assertEqual(out["periods"]["30d"]["prompt"], 10 + 100 + 1 + 2 + 4)
        self.assertEqual(out["periods"]["all"]["prompt"], 10 + 100 + 1 + 2 + 4 + 8)

    def test_partial_last_line_waits_for_its_newline(self):
        now = utc("2026-10-02T20:00:00Z")
        line = json.dumps(sample("2026-10-02T19:55:00Z", prompt=40, gen=4, reqs=1))
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:54:00Z", prompt=1, gen=1, reqs=1)], partial=line[:30])
        self.assertEqual(self.run_once(now)["periods"]["all"]["total"], 2)
        with open(self.tele / "2026-10-02.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line[30:] + "\n")
        self.assertEqual(self.run_once(now)["periods"]["all"]["total"], 2 + 44)

    def test_ledger_outlives_telemetry_files_and_rewrites_do_not_double_count(self):
        now = utc("2026-10-02T20:00:00Z")
        rows = [sample("2026-10-02T19:50:00Z", prompt=300, gen=30, reqs=1)]
        self.write("2026-10-02.jsonl", rows)
        self.assertEqual(self.run_once(now)["periods"]["all"]["total"], 330)
        self.write("2026-10-02.jsonl", rows)  # same content rewritten
        self.assertEqual(self.run_once(now)["periods"]["all"]["total"], 330)
        os.unlink(self.tele / "2026-10-02.jsonl")  # retention removed the file
        self.assertEqual(self.run_once(now)["periods"]["all"]["total"], 330)

    def test_bad_lines_and_missing_telemetry_do_not_break_the_run(self):
        now = utc("2026-10-02T20:00:00Z")
        with open(self.tele / "2026-10-02.jsonl", "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write(json.dumps(sample("2026-10-02T19:50:00Z", prompt="x", gen=-5, reqs=None)) + "\n")
            fh.write(json.dumps(sample("2026-10-02T19:51:00Z", prompt=9, gen=1, reqs=1)) + "\n")
        self.assertEqual(self.run_once(now)["periods"]["all"]["total"], 10)
        empty = ct.main.__globals__["telemetry_dir"](str(self.tele / "missing"))
        self.assertFalse(empty.exists())

    def test_ledger_file_is_private(self):
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:50:00Z", prompt=1, gen=1, reqs=1)])
        self.run_once(utc("2026-10-02T20:00:00Z"))
        mode = stat.S_IMODE(os.stat(self.state / "cluster-ledger.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_tensorfold_samples_count_requests_from_completed(self):
        now = utc("2026-10-02T20:00:00Z")
        self.write("2026-10-02.jsonl", [
            sample("2026-10-02T19:50:00Z", prompt=900, gen=90, reqs=None, completed=7),
            sample("2026-10-02T19:51:00Z", prompt=100, gen=10, reqs=None, completed=0),
            sample("2026-10-02T19:52:00Z", prompt=5, gen=5, reqs=2, completed=2),
        ])
        out = self.run_once(now)
        self.assertEqual(out["periods"]["today"]["requests"], 9)
        self.assertEqual(out["requests_partial"], [])

    def test_requests_are_null_not_zero_when_engine_writes_no_count(self):
        now = utc("2026-10-02T20:00:00Z")
        self.write("2026-10-02.jsonl", [
            sample("2026-10-02T19:50:00Z", prompt=900, gen=90, reqs=None),
            sample("2026-10-02T19:51:00Z", prompt=100, gen=10, reqs=None),
        ])
        out = self.run_once(now)
        self.assertEqual(out["periods"]["today"], {"prompt": 1000, "generation": 100, "total": 1100, "requests": None})
        self.assertIsNone(out["periods"]["all"]["requests"])
        self.assertEqual(out["requests_partial"], [])
        idle = self.run_once(utc("2026-10-04T20:00:00Z"))
        self.assertEqual(idle["periods"]["today"]["requests"], 0, "no traffic is a real 0")

    def test_mixed_days_report_a_lower_bound(self):
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:50:00Z", prompt=10, gen=1, reqs=None)])
        self.write("2026-10-03.jsonl", [sample("2026-10-03T19:50:00Z", prompt=10, gen=1, reqs=4)])
        out = self.run_once(utc("2026-10-03T20:00:00Z"))
        self.assertEqual(out["periods"]["today"]["requests"], 4)
        self.assertEqual(out["periods"]["all"]["requests"], 4)
        self.assertEqual(out["requests_partial"], ["7d", "30d", "all"])

    def test_old_three_column_ledger_marks_zero_request_days_unknown(self):
        now = utc("2026-10-02T20:00:00Z")
        self.state.mkdir()
        legacy = {"version": 1, "first_ts": utc("2026-10-01T10:00:00Z"), "files": {},
                  "daily": {"2026-10-01": [500, 50, 12], "2026-10-02": [300, 30, 0]}}
        (self.state / "cluster-ledger.json").write_text(json.dumps(legacy), encoding="utf-8")
        out = self.run_once(now)
        self.assertIsNone(out["periods"]["today"]["requests"])
        self.assertEqual(out["periods"]["7d"]["requests"], 12)
        self.assertEqual(out["requests_partial"], ["7d", "30d", "all"])
        self.write("2026-10-02.jsonl", [sample("2026-10-02T19:50:00Z", prompt=1, gen=1, reqs=None, completed=3)])
        grown = self.run_once(now)
        self.assertEqual(grown["periods"]["today"]["requests"], 3)
        self.assertIn("today", grown["requests_partial"])


if __name__ == "__main__":
    unittest.main()
