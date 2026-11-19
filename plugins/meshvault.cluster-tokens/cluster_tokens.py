#!/usr/bin/env python3
"""cluster_tokens: token totals for the local open-source cluster.

The cluster serves one model, so one pair of counters covers everything it does.
`cluster-telemetry` already samples the vLLM counters once a minute and writes the
per-sample change (`d_prompt_tokens`, `d_generation_tokens`, `d_requests_total`,
or `completed` when the engine leaves `d_requests_total` blank).
This script adds those changes into a small per-day ledger that outlives the
telemetry files (60 day retention) and prints one JSON line for the bar widget.

Read-only on telemetry. Standard library only. Never prints prompts or replies,
because none are ever read: telemetry holds counters only.

Known gap: the telemetry leaves the change blank for the first sample after an
engine restart, so tokens served in that single minute are not counted.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import time
from pathlib import Path

VERSION = 1
PERIODS = (("today", 1), ("7d", 7), ("30d", 30), ("all", None))
STALE_AFTER_S = 180
TAIL_BYTES = 16384
TPS_WINDOW = 12


def state_dir(arg):
    if arg:
        return Path(arg)
    env = os.environ.get("MESHVAULT_TOKENS_STATE")
    if env:
        return Path(env)
    return Path.home() / ".local" / "state" / "meshvault-tokens"


def telemetry_dir(arg):
    if arg:
        return Path(arg)
    env = os.environ.get("CLUSTER_TELEMETRY_DIR")
    if env:
        return Path(env)
    return Path.home() / "ops" / "state" / "cluster-telemetry"


def parse_ts(text):
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def local_day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def whole(value):
    """A non-negative integer count, or 0 for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if value > 0 else 0


def load_ledger(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        data = {}
    data["version"] = VERSION
    if not isinstance(data.get("daily"), dict):
        data["daily"] = {}
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    if not isinstance(data.get("first_ts"), (int, float)):
        data["first_ts"] = None
    for row in data["daily"].values():
        # Rows written before request tracking have three columns. A day that
        # served tokens but counted 0 requests could not see requests: mark it.
        if isinstance(row, list) and len(row) == 3:
            row.append(1 if row[2] == 0 and row[0] + row[1] > 0 else 0)
    return data


def save_ledger(path, ledger):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, separators=(",", ":"))
    os.replace(tmp, path)


def request_count(d):
    """Requests finished in one sample, or None when the engine wrote no count.

    vLLM engines write `d_requests_total` (finish reasons summed). TensorFold
    leaves it blank and writes `completed` (change in the e2e latency count)
    instead; the two are equal in every sample that has both."""
    for key in ("d_requests_total", "completed"):
        value = d.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return whole(value)
    return None


def apply_line(ledger, state, raw):
    try:
        d = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(d, dict):
        return False
    ts = parse_ts(d.get("ts"))
    if ts is None or ts <= state["last_ts"]:
        return False
    state["last_ts"] = ts
    if ledger["first_ts"] is None or ts < ledger["first_ts"]:
        ledger["first_ts"] = ts
    prompt = whole(d.get("d_prompt_tokens"))
    gen = whole(d.get("d_generation_tokens"))
    reqs = request_count(d)
    if prompt or gen or reqs:
        row = ledger["daily"].setdefault(local_day(ts), [0, 0, 0, 0])
        row[0] += prompt
        row[1] += gen
        row[2] += reqs or 0
        if reqs is None and (prompt or gen):
            row[3] += 1
    return True


def ingest(ledger, tdir, errors):
    try:
        names = sorted(p.name for p in tdir.glob("*.jsonl"))
    except OSError as exc:
        errors.append({"source": "telemetry", "message": "cannot list telemetry: %s" % exc.__class__.__name__})
        return
    for name in names:
        path = tdir / name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        state = ledger["files"].get(name)
        if not isinstance(state, dict):
            state = {"offset": 0, "last_ts": 0.0}
        offset = state.get("offset")
        if not isinstance(offset, int) or offset < 0 or offset > size:
            offset = 0  # file was rewritten; the per-file last_ts guard prevents double counting
        if not isinstance(state.get("last_ts"), (int, float)):
            state["last_ts"] = 0.0
        if offset == size:
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(size - offset)
        except OSError as exc:
            errors.append({"source": "telemetry", "message": "cannot read %s: %s" % (name, exc.__class__.__name__)})
            continue
        end = data.rfind(b"\n")
        if end < 0:
            continue  # only a partial line so far
        for raw in data[: end + 1].splitlines():
            apply_line(ledger, state, raw)
        state["offset"] = offset + end + 1
        ledger["files"][name] = state
    for gone in [n for n in ledger["files"] if n not in names]:
        del ledger["files"][gone]


def newest_tail(tdir):
    """Return the last few complete parsed telemetry lines of the newest file."""
    try:
        files = sorted(tdir.glob("*.jsonl"))
    except OSError:
        return []
    for path in reversed(files):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - TAIL_BYTES))
                data = fh.read()
        except OSError:
            continue
        lines = data.split(b"\n")
        if not data.endswith(b"\n"):
            lines = lines[:-1]
        if size > TAIL_BYTES and lines:
            lines = lines[1:]
        out = []
        for raw in lines:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        if out:
            return out[-TPS_WINDOW:]
    return []


def period_rows(daily, now, count):
    if count is None:
        return list(daily.values())
    today = dt.date.fromtimestamp(now)
    days = {(today - dt.timedelta(days=i)).isoformat() for i in range(count)}
    return [daily[d] for d in daily if d in days]


def period_totals(daily, now, count):
    """Token sums plus `requests`: the summed count, or null when tokens were
    served in the period but no sample carried a request count (never a false 0)."""
    rows = period_rows(daily, now, count)
    prompt = sum(r[0] for r in rows)
    gen = sum(r[1] for r in rows)
    reqs = sum(r[2] for r in rows)
    blind = sum(r[3] for r in rows)
    return {"prompt": prompt, "generation": gen, "total": prompt + gen,
            "requests": None if reqs == 0 and blind > 0 else reqs}


def read_created(tdir):
    try:
        raw = json.loads((tdir / "last.json").read_text(encoding="utf-8"))
        created = raw["raw"]["created"]
        return float(created) if isinstance(created, (int, float)) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def engine_block(tail, tdir, now):
    if not tail:
        return {"up": False, "stale": True, "model": None, "last_sample": None}
    last = tail[-1]
    ts = parse_ts(last.get("ts"))
    stale = ts is None or now - ts > STALE_AFTER_S
    up = bool(last.get("engine_up")) and not stale
    tps = [d["decode_tps_per_req"] for d in tail
           if isinstance(d.get("decode_tps_per_req"), (int, float)) and d["decode_tps_per_req"] > 0]
    created = read_created(tdir)
    block = {
        "up": up,
        "stale": stale,
        "error": last.get("error"),
        "model": last.get("served_model"),
        "max_model_len": last.get("max_model_len"),
        "running": last.get("running"),
        "waiting": last.get("waiting"),
        "kv_usage": last.get("kv_usage"),
        "last_sample": last.get("ts"),
        "decode_tps": round(sum(tps) / len(tps), 1) if tps else None,
        "uptime_s": int(now - created) if up and created and created < now else None,
    }
    if block["model"] is None:
        for d in reversed(tail):
            if d.get("served_model"):
                block["model"] = d["served_model"]
                break
    return block


def partial_periods(daily, now):
    """Periods whose request count is a lower bound: some traffic had no count."""
    out = []
    for name, count in PERIODS:
        rows = period_rows(daily, now, count)
        if sum(r[2] for r in rows) > 0 and sum(r[3] for r in rows) > 0:
            out.append(name)
    return out


def build(ledger, tail, tdir, now, errors):
    return {
        "version": VERSION,
        "generated_at": round(now, 3),
        "day": local_day(now),
        "since": ledger["first_ts"],
        "engine": engine_block(tail, tdir, now),
        "periods": {name: period_totals(ledger["daily"], now, count) for name, count in PERIODS},
        "requests_partial": partial_periods(ledger["daily"], now),
        "errors": errors,
    }


def run(args):
    sdir = state_dir(args.state_dir)
    try:
        sdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        print("cannot create state dir: %s" % exc, file=sys.stderr)
        return 2
    now = args.now if args.now is not None else time.time()
    tdir = telemetry_dir(args.telemetry_dir)
    errors = []
    lock_fd = os.open(sdir / "cluster.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        ledger_path = sdir / "cluster-ledger.json"
        ledger = load_ledger(ledger_path)
        before = json.dumps(ledger, sort_keys=True)
        ingest(ledger, tdir, errors)
        if json.dumps(ledger, sort_keys=True) != before:
            save_ledger(ledger_path, ledger)
        out = build(ledger, newest_tail(tdir), tdir, now, errors)
    finally:
        os.close(lock_fd)
    print(json.dumps(out, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Token totals for the local open-source cluster.")
    parser.add_argument("--state-dir")
    parser.add_argument("--telemetry-dir")
    parser.add_argument("--now", type=float)
    parser.add_argument("--pretty", action="store_true")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
