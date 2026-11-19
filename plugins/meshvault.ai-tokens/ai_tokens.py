#!/usr/bin/env python3
"""Total AI token usage per provider from local logs for the Omarchy bar.

Reads Claude Code, Codex CLI and Grok CLI logs plus the OMP and Hermes
SQLite databases, all read-only. Keeps daily buckets in a ledger so each
run only parses new data. Prints exactly one JSON line (version 1).

Only numbers, timestamps, model names and ids are extracted. Nothing from
prompts, replies or tool output is stored or printed.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse

VERSION = 1
LEDGER_NAME = "ai-ledger.json"
SEEN_NAME = "ai-claude-seen.bin"
LOCK_NAME = "ai-ledger.lock"
KEY_BYTES = 8
CHUNK_BYTES = 8 << 20
OMP_SYNC_INTERVAL = 300.0
OMP_SYNC_TIMEOUT = 60.0
DB_TIMEOUT = 5.0
LOCK_WAIT = 5.0  # budgeted runs wait this long for another run, then show saved totals
TOP_MODELS = 8

PROVIDERS = {
    "anthropic": {"label": "Anthropic", "short": "CLD", "sources": ["claude_code", "omp", "hermes"]},
    "codex": {"label": "Codex", "short": "CDX", "sources": ["codex_cli", "omp", "hermes"]},
    "grok": {"label": "Grok", "short": "GRK", "sources": ["grok_cli", "omp", "hermes"]},
}
OMP_MAP = {"anthropic": "anthropic", "openai-codex": "codex", "xai-oauth": "grok", "xai": "grok"}
HERMES_MAP = {
    "anthropic": "anthropic",
    "claude-subscription-directsdk-experimental": "anthropic",
    "openai-codex": "codex",
    "xai-oauth": "grok",
    "xai": "grok",
}
FILE_SOURCES = ("grok_cli", "codex_cli", "claude_code")
PERIODS = ("today", "7d", "30d", "all")


# ---------------------------------------------------------------- helpers

def num(value):
    """Coerce a token counter to a non-negative int; junk becomes 0."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def parse_iso(text):
    """ISO 8601 text to epoch seconds. Naive times are taken as UTC."""
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp()


def local_day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch))


def short_error(exc):
    """A short error text that never carries file paths or file content."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or 'os error'}"
    if isinstance(exc, sqlite3.Error):
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    return type(exc).__name__


def ro_connect(path):
    uri = "file:" + urllib.parse.quote(os.path.abspath(path), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=DB_TIMEOUT)


def new_ledger():
    return {
        "version": VERSION,
        "daily": {},
        "files": {name: {} for name in FILE_SOURCES},
        "grok_turns": {},
        "hermes_snap": {},
        "omp_last_sync": 0,
        "last_event": {},
        "claude_seen_count": 0,
        "pending_files": 0,
    }


class Run:
    """State for one collector run: ledger, clock, budget and errors."""

    def __init__(self, ledger, now, deadline, progress):
        self.ledger = ledger
        self.now = now
        self.deadline = deadline
        self.progress = progress
        self.progressed = False
        self.errors = {}
        self.pending = 0
        self.seen = None
        self.new_keys = []
        self.day_cache = {}

    def error(self, source, message):
        prior = self.errors.get(source)
        self.errors[source] = message if prior is None else f"{prior}; {message}"

    def out_of_time(self):
        """True once the budget is spent, but only after some progress so a
        series of budgeted runs always moves the backfill forward."""
        return self.deadline is not None and self.progressed and time.monotonic() >= self.deadline

    def day(self, epoch):
        # Events cluster in time, so cache by the hour to avoid many localtime calls.
        hour = int(epoch // 3600)
        hit = self.day_cache.get(hour)
        if hit is None:
            hit = local_day(epoch)
            if local_day(hour * 3600) == local_day(hour * 3600 + 3599):
                self.day_cache[hour] = hit
        return hit

    def add(self, epoch, provider, source, model, inp, out, cw, cr, req):
        day = self.day(epoch)
        buckets = self.ledger["daily"].setdefault(day, {}).setdefault(provider, {}).setdefault(source, {})
        row = buckets.get(model)
        if row is None:
            row = buckets[model] = [0, 0, 0, 0, 0]
        row[0] += inp
        row[1] += out
        row[2] += cw
        row[3] += cr
        row[4] += req
        last = self.ledger["last_event"].setdefault(provider, {})
        if epoch > (last.get(source) or 0):
            last[source] = epoch

    def say(self, text):
        if self.progress:
            print(text, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- file scanning

def scan_tree(root, accept):
    """Recursive os.scandir walk. Returns {path: (size, mtime_ns)}."""
    found = {}
    stack = [root]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif accept(entry.name) and entry.is_file():
                            st = entry.stat()
                            found[entry.path] = (st.st_size, st.st_mtime_ns)
                    except OSError:
                        continue
        except OSError:
            if top == root:
                raise
    return found


def scan_grok(root):
    """Files matching <root>/*/*/usage.json."""
    found = {}
    with os.scandir(root) as level1:
        for a in level1:
            if not a.is_dir():
                continue
            try:
                with os.scandir(a.path) as level2:
                    for b in level2:
                        if not b.is_dir():
                            continue
                        path = os.path.join(b.path, "usage.json")
                        try:
                            st = os.stat(path)
                        except OSError:
                            continue
                        found[path] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return found


def read_lines(run, path, offset):
    """Yield complete lines from offset on. The generator's final value
    (via StopIteration) is (new_offset, reached_eof)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        tail = b""
        while True:
            chunk = fh.read(CHUNK_BYTES)
            if not chunk:
                return offset, True
            data = tail + chunk
            cut = data.rfind(b"\n")
            if cut < 0:
                tail = data
                continue
            tail = data[cut + 1:]
            for line in data[:cut].split(b"\n"):
                yield line
            offset += cut + 1  # offset marks the start of data, so this is the next unread line
            run.progressed = True
            # A short chunk means the next read hits EOF, so finish the file.
            if len(chunk) == CHUNK_BYTES and run.out_of_time():
                return offset, False


def drive_lines(run, path, offset, handle):
    """Feed complete lines to handle(line). Returns (offset, reached_eof)."""
    gen = read_lines(run, path, offset)
    while True:
        try:
            line = next(gen)
        except StopIteration as stop:
            return stop.value
        handle(line)


# ---------------------------------------------------------------- claude_code

def load_seen(run, state_dir):
    """Load committed Claude dedupe keys. Bytes past the committed count belong
    to a run that never saved its ledger, so they are dropped."""
    path = os.path.join(state_dir, SEEN_NAME)
    want = int(run.ledger.get("claude_seen_count") or 0) * KEY_BYTES
    try:
        with open(path, "rb") as fh:
            blob = fh.read(want)
    except FileNotFoundError:
        blob = b""
    if len(blob) < want:
        run.error("claude_code", "dedupe file shorter than ledger; some keys lost")
        run.ledger["claude_seen_count"] = len(blob) // KEY_BYTES
        blob = blob[: run.ledger["claude_seen_count"] * KEY_BYTES]
    run.seen = {blob[i:i + KEY_BYTES] for i in range(0, len(blob), KEY_BYTES)}


def claude_handler(run, counters):
    seen = run.seen
    new_keys = run.new_keys
    blake = hashlib.blake2b

    def handle(line):
        if b'"usage"' not in line or b"assistant" not in line:
            return
        try:
            rec = json.loads(line)
        except ValueError:
            counters["bad"] += 1
            return
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            return
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        usage = msg.get("usage")
        model = msg.get("model")
        mid = msg.get("id")
        rid = rec.get("requestId")
        stamp = rec.get("timestamp")
        if not (isinstance(usage, dict) and model and mid and rid and stamp):
            return
        if model == "<synthetic>":
            return
        inp = num(usage.get("input_tokens"))
        out = num(usage.get("output_tokens"))
        cw = num(usage.get("cache_creation_input_tokens"))
        cr = num(usage.get("cache_read_input_tokens"))
        if inp + out + cw + cr == 0:
            return
        key = blake(f"{mid}\x00{rid}".encode(), digest_size=KEY_BYTES).digest()
        if key in seen:
            return
        try:
            epoch = parse_iso(stamp)
        except (TypeError, ValueError):
            counters["bad"] += 1
            return
        seen.add(key)
        new_keys.append(key)
        run.add(epoch, "anthropic", "claude_code", str(model), inp, out, cw, cr, 1)

    return handle


# ---------------------------------------------------------------- codex_cli

def codex_handler(run, state, counters):
    """state: {"base": [input, cached, output] or None, "model": str,
    "replay": [input, cached, output] or None}."""

    def handle(line):
        is_ctx = b'"turn_context"' in line
        if not is_ctx and b'"token_count"' not in line:
            return
        try:
            rec = json.loads(line)
        except ValueError:
            counters["bad"] += 1
            return
        if not isinstance(rec, dict):
            return
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            return
        kind = rec.get("type")
        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                state["model"] = model
            return
        if kind != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if not isinstance(info, dict):
            return
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return
        cur = [num(total.get("input_tokens")), num(total.get("cached_input_tokens")), num(total.get("output_tokens"))]
        hw = state.get("replay")
        if hw is not None:
            # File was rewritten from the start: skip values already counted.
            state["replay_last"] = cur
            if any(c < h for c, h in zip(cur, hw)):
                return
            delta = [c - h for c, h in zip(cur, hw)]
            state["replay"] = None
            state.pop("replay_last", None)
        else:
            base = state.get("base") or [0, 0, 0]
            if any(c < b for c, b in zip(cur, base)):
                delta = cur
            else:
                delta = [c - b for c, b in zip(cur, base)]
        state["base"] = cur
        if not any(delta):
            return
        try:
            epoch = parse_iso(rec.get("timestamp"))
        except (TypeError, ValueError):
            counters["bad"] += 1
            return
        d_in, d_cached, d_out = delta
        run.add(epoch, "codex", "codex_cli", state.get("model") or "codex",
                max(0, d_in - d_cached), d_out, 0, d_cached, 1)

    return handle


# ---------------------------------------------------------------- grok_cli

def strip_build(model):
    model = str(model or "grok")
    return model[: -len("-build")] if model.endswith("-build") else model


def grok_file(run, path):
    with open(path, "rb") as fh:
        doc = json.loads(fh.read())
    if not isinstance(doc, dict):
        raise ValueError("not an object")
    sid = str(doc.get("sessionId") or path)
    turns_seen = run.ledger["grok_turns"].setdefault(sid, [])
    done = set(turns_seen)
    for turn in doc.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        number = turn.get("turnNumber")
        ended = turn.get("endedAt")
        if not isinstance(number, int) or number in done or not ended:
            continue
        epoch = parse_iso(ended)
        usage = turn.get("modelUsage")
        if isinstance(usage, dict) and usage:
            parts = [(name, fields) for name, fields in usage.items() if isinstance(fields, dict)]
        else:
            parts = [(turn.get("primaryModelId"), turn)]
        for name, fields in parts:
            inp = num(fields.get("inputTokens"))
            cached = num(fields.get("cachedReadTokens"))
            out = num(fields.get("outputTokens"))
            cw = num(fields.get("cacheCreationTokens"))
            calls = num(fields.get("modelCalls")) or 1
            if inp + out + cw + cached == 0:
                continue
            run.add(epoch, "grok", "grok_cli", strip_build(name), max(0, inp - cached), out, cw, cached, calls)
        done.add(number)
        turns_seen.append(number)
    turns_seen.sort()


# ---------------------------------------------------------------- file sources driver

def file_sources(run, state_dir, env):
    home = os.path.expanduser("~")
    roots = {
        "claude_code": env.get("CLAUDE_PROJECTS_DIR") or os.path.join(home, ".claude", "projects"),
        "codex_cli": env.get("CODEX_SESSIONS_DIR") or os.path.join(home, ".codex", "sessions"),
        "grok_cli": env.get("GROK_SESSIONS_DIR") or os.path.join(home, ".grok", "sessions"),
    }
    work = []
    for source in FILE_SOURCES:
        root = roots[source]
        try:
            if source == "grok_cli":
                found = scan_grok(root)
            elif source == "codex_cli":
                found = scan_tree(root, lambda n: n.startswith("rollout-") and n.endswith(".jsonl"))
            else:
                found = scan_tree(root, lambda n: n.endswith(".jsonl"))
        except FileNotFoundError:
            run.error(source, "source directory missing")
            continue
        except OSError as exc:
            run.error(source, short_error(exc))
            continue
        known = run.ledger["files"].setdefault(source, {})
        for gone in [p for p in known if p not in found]:
            del known[gone]
        changed = []
        for path, (size, mtime) in found.items():
            prev = known.get(path)
            if prev and prev[0] == size and prev[1] == mtime:
                continue
            changed.append((mtime, path, size))
        changed.sort(reverse=True)
        work.extend((source, path, size, mtime) for mtime, path, size in changed)

    total = len(work)
    counters = {s: {"bad": 0, "unreadable": 0} for s in FILE_SOURCES}
    last_note = time.monotonic()
    for index, (source, path, size, mtime) in enumerate(work):
        if run.out_of_time():
            run.pending = total - index
            break
        known = run.ledger["files"][source]
        try:
            if source == "grok_cli":
                try:
                    grok_file(run, path)
                except (ValueError, TypeError, AttributeError):
                    # Usually a rewrite caught mid-write; retry next run.
                    counters[source]["bad"] += 1
                    run.progressed = True
                    continue
                known[path] = [size, mtime]
                run.progressed = True
            elif source == "claude_code":
                if run.seen is None:
                    load_seen(run, state_dir)
                prev = known.get(path)
                offset = prev[2] if prev else 0
                if size < offset:
                    offset = 0
                offset, eof = drive_lines(run, path, offset, claude_handler(run, counters[source]))
                known[path] = [size, mtime, offset] if eof else [-1, -1, offset]
                if not eof:
                    run.pending = total - index
                    break
            else:
                prev = known.get(path)
                state = {"base": None, "model": "codex", "replay": None}
                offset = 0
                if prev:
                    offset = prev[2]
                    state = {"base": prev[3], "model": prev[4], "replay": prev[5]}
                if size < offset:
                    offset = 0
                    state["replay"] = state["base"]
                offset, eof = drive_lines(run, path, offset, codex_handler(run, state, counters[source]))
                if eof and state["replay"] is not None:
                    # Reached the end of a rewritten file: trust its last value.
                    state["base"] = state.pop("replay_last", None) or state["replay"]
                    state["replay"] = None
                state.pop("replay_last", None)
                entry = [size, mtime] if eof else [-1, -1]
                known[path] = entry + [offset, state["base"], state["model"], state["replay"]]
                if not eof:
                    run.pending = total - index
                    break
        except OSError:
            counters[source]["unreadable"] += 1
            continue
        except Exception:  # an odd file must not stop the rest
            counters[source]["bad"] += 1
            continue
        if run.progress and time.monotonic() - last_note >= 2:
            last_note = time.monotonic()
            run.say(f"backfill: {index + 1}/{total} files ({source})")
    for source, c in counters.items():
        if c["bad"]:
            run.error(source, f"{c['bad']} malformed lines or files skipped")
        if c["unreadable"]:
            run.error(source, f"{c['unreadable']} unreadable files")


# ---------------------------------------------------------------- hermes

def hermes_source(run, env):
    path = env.get("HERMES_STATE_DB") or os.path.expanduser("~/.hermes/state.db")
    if not os.path.exists(path):
        run.error("hermes", "database missing")
        return
    con = ro_connect(path)
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        calls = "api_call_count" if "api_call_count" in cols else "NULL"
        marks = ",".join("?" for _ in HERMES_MAP)
        rows = con.execute(
            "SELECT id, billing_provider, model, started_at, ended_at, input_tokens, output_tokens, "
            f"cache_write_tokens, cache_read_tokens, {calls} FROM sessions "
            f"WHERE billing_provider IN ({marks})",
            list(HERMES_MAP),
        ).fetchall()
    finally:
        con.close()
    snaps = run.ledger["hermes_snap"]
    for sid, billing, model, started, ended, inp, out, cw, cr, api_calls in rows:
        provider = HERMES_MAP.get(billing)
        if provider is None or sid is None:
            continue
        vals = [num(inp), num(out), num(cw), num(cr)]
        model = str(model) if model else "?"
        prior = snaps.get(sid)
        snaps[sid] = vals
        if prior is None:
            if any(vals):
                when = ended if ended is not None else started
                try:
                    when = float(when)
                except (TypeError, ValueError):
                    when = run.now
                run.add(when, provider, "hermes", model, *vals, num(api_calls) or 1)
            continue
        delta = [max(0, v - p) for v, p in zip(vals, prior)]
        if any(delta):
            run.add(run.now, provider, "hermes", model, *delta, 1)


# ---------------------------------------------------------------- omp

def omp_start_sync(run, env, enabled):
    if not enabled or run.now - float(run.ledger.get("omp_last_sync") or 0) < OMP_SYNC_INTERVAL:
        return None
    run.ledger["omp_last_sync"] = run.now
    try:
        return subprocess.Popen(
            [env.get("OMP_BIN") or "omp", "stats", "--summary"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ), time.monotonic()
    except OSError as exc:
        run.error("omp", "stats sync failed to start: " + short_error(exc))
        return None


def omp_finish_sync(run, sync):
    if sync is None:
        return
    proc, started = sync
    hard = started + OMP_SYNC_TIMEOUT
    until = hard if run.deadline is None else min(hard, max(run.deadline, time.monotonic()))
    try:
        code = proc.wait(timeout=max(0.0, until - time.monotonic()))
    except subprocess.TimeoutExpired:
        if time.monotonic() >= hard - 0.05:
            proc.kill()
            proc.wait()
            run.error("omp", "stats sync timed out")
        # Otherwise the budget ran out first: leave the sync running and
        # read the database as it stands.
        return
    if code:
        run.error("omp", f"stats sync exited {code}")


def omp_source(run, env):
    path = env.get("OMP_STATS_DB") or os.path.expanduser("~/.omp/stats.db")
    if not os.path.exists(path):
        run.error("omp", "database missing")
        return
    con = ro_connect(path)
    try:
        marks = ",".join("?" for _ in OMP_MAP)
        rows = con.execute(
            "SELECT date(timestamp / 1000, 'unixepoch', 'localtime') AS d, provider, model, "
            "SUM(input_tokens), SUM(output_tokens), SUM(cache_write_tokens), SUM(cache_read_tokens), "
            f"COUNT(*), MAX(timestamp) FROM messages WHERE provider IN ({marks}) "
            "GROUP BY d, provider, model",
            list(OMP_MAP),
        ).fetchall()
    finally:
        con.close()
    # Recomputed from scratch: drop the old omp slice, then rebuild it.
    daily = run.ledger["daily"]
    for day in list(daily):
        for provider in list(daily[day]):
            daily[day][provider].pop("omp", None)
    for prov_last in run.ledger["last_event"].values():
        prov_last.pop("omp", None)
    for day, raw_provider, model, inp, out, cw, cr, count, last_ms in rows:
        provider = OMP_MAP.get(raw_provider)
        if provider is None or not day:
            continue
        bucket = daily.setdefault(day, {}).setdefault(provider, {}).setdefault("omp", {})
        row = bucket.setdefault(str(model) if model else "?", [0, 0, 0, 0, 0])
        for idx, value in enumerate((inp, out, cw, cr, count)):
            row[idx] += num(value)
        if last_ms:
            last = run.ledger["last_event"].setdefault(provider, {})
            last["omp"] = max(last.get("omp") or 0, num(last_ms) / 1000.0)


# ---------------------------------------------------------------- ledger io

def prune_empty(ledger):
    daily = ledger["daily"]
    for day in list(daily):
        for provider in list(daily[day]):
            for source in list(daily[day][provider]):
                if not daily[day][provider][source]:
                    del daily[day][provider][source]
            if not daily[day][provider]:
                del daily[day][provider]
        if not daily[day]:
            del daily[day]


def load_ledger(state_dir, readonly=False):
    """Returns (ledger, problem). A broken ledger is set aside (unless
    readonly) and replaced by an empty one; the Claude dedupe file then
    resets too, because its committed key count lives in the ledger."""
    path = os.path.join(state_dir, LEDGER_NAME)
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read())
    except FileNotFoundError:
        return new_ledger(), None
    except (OSError, ValueError) as exc:
        if not readonly:
            try:
                os.replace(path, f"{path}.corrupt-{int(time.time())}")
            except OSError:
                pass
        return new_ledger(), "ledger unreadable, rebuilt: " + short_error(exc)
    base = new_ledger()
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return base, "ledger version mismatch, rebuilt"
    for key, value in base.items():
        kind = (int, float) if isinstance(value, int) else type(value)
        if not isinstance(data.get(key), kind):
            data[key] = value
    for name in FILE_SOURCES:
        data["files"].setdefault(name, {})
    return data, None


def write_atomic(path, payload):
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".ai-ledger.", suffix=".tmp", dir=folder)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def save(run, state_dir):
    """Commit order: append new dedupe keys, then replace the ledger, whose
    claude_seen_count marks how many keys are committed."""
    ledger = run.ledger
    if run.seen is not None:
        seen_path = os.path.join(state_dir, SEEN_NAME)
        committed = int(ledger.get("claude_seen_count") or 0)
        fd = os.open(seen_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.ftruncate(fd, committed * KEY_BYTES)
            os.lseek(fd, 0, os.SEEK_END)
            if run.new_keys:
                os.write(fd, b"".join(run.new_keys))
            os.fsync(fd)
        finally:
            os.close(fd)
        ledger["claude_seen_count"] = committed + len(run.new_keys)
        run.new_keys = []
    ledger["pending_files"] = run.pending
    prune_empty(ledger)
    payload = json.dumps(ledger, separators=(",", ":"), sort_keys=True).encode()
    write_atomic(os.path.join(state_dir, LEDGER_NAME), payload)


# ---------------------------------------------------------------- output

def vec_to_t(vec):
    inp, out, cw, cr, req = vec
    return {"input": inp, "output": out, "cache_write": cw, "cache_read": cr,
            "total": inp + out + cw, "requests": req}


def build_output(ledger, now, errors, pending):
    today_date = dt.date.fromisoformat(local_day(now))
    today = today_date.isoformat()
    start7 = (today_date - dt.timedelta(days=6)).isoformat()
    start30 = (today_date - dt.timedelta(days=29)).isoformat()

    def periods_of(day):
        hit = ["all"]
        if start30 <= day <= today:
            hit.append("30d")
            if day >= start7:
                hit.append("7d")
                if day == today:
                    hit.append("today")
        return hit

    providers = {}
    for provider, meta in PROVIDERS.items():
        src = {s: {p: [0, 0, 0, 0, 0] for p in PERIODS} for s in meta["sources"]}
        models = {}
        for day, per_provider in ledger["daily"].items():
            slices = per_provider.get(provider)
            if not slices:
                continue
            hit = periods_of(day)
            for source, per_model in slices.items():
                if source not in src:
                    continue
                for model, row in per_model.items():
                    mt = models.setdefault(model, {p: 0 for p in PERIODS})
                    total = row[0] + row[1] + row[2]
                    for period in hit:
                        acc = src[source][period]
                        for i in range(5):
                            acc[i] += row[i]
                        mt[period] += total
        periods = {}
        for period in PERIODS:
            acc = [0, 0, 0, 0, 0]
            for s in src.values():
                for i in range(5):
                    acc[i] += s[period][i]
            periods[period] = vec_to_t(acc)
        ranked = sorted(models.items(), key=lambda kv: (-kv[1]["all"], kv[0]))[:TOP_MODELS]
        last = [v for v in (ledger["last_event"].get(provider) or {}).values() if v]
        providers[provider] = {
            "label": meta["label"],
            "short": meta["short"],
            "periods": periods,
            "sources": {s: {p: vec_to_t(v) for p, v in per.items()} for s, per in src.items()},
            "models": [dict(model=m, **{p: t[p] for p in PERIODS}) for m, t in ranked],
            "last_event_at": max(last) if last else None,
        }
    return {
        "version": VERSION,
        "generated_at": now,
        "day": today,
        "backfill": {"complete": pending == 0, "pending_files": pending},
        "providers": providers,
        "errors": [{"source": s, "message": m} for s, m in errors.items()],
    }


def emit(doc, pretty):
    if pretty:
        text = json.dumps(doc, indent=2)
    else:
        text = json.dumps(doc, separators=(",", ":"))
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- main

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Total AI token usage per provider for the Omarchy bar.")
    parser.add_argument("--state-dir")
    parser.add_argument("--budget", type=float, default=20.0)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--no-omp-sync", action="store_true")
    parser.add_argument("--now", type=float)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if args.budget < 0 or args.budget != args.budget:
        parser.error("--budget must be 0 or more")
    if args.now is not None and not (0 <= args.now < 1e11):
        parser.error("--now must be an epoch in seconds")
    return args


def acquire_lock(lock_path, wait_seconds):
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if wait_seconds is None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    give_up = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= give_up:
                os.close(fd)
                return None
            time.sleep(0.1)


def main(argv=None, env=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    started = time.monotonic()
    now = time.time() if args.now is None else args.now
    budget = None if (args.backfill or args.budget == 0) else args.budget
    deadline = None if budget is None else started + budget

    state_dir = os.path.expanduser(
        args.state_dir or env.get("MESHVAULT_TOKENS_STATE") or "~/.local/state/meshvault-tokens")
    try:
        if not os.path.isdir(state_dir):
            os.makedirs(state_dir, mode=0o700)
            os.chmod(state_dir, 0o700)
        if not os.access(state_dir, os.W_OK | os.X_OK):
            raise PermissionError(13, "Permission denied")
        lock_fd = acquire_lock(os.path.join(state_dir, LOCK_NAME),
                               None if budget is None else min(budget, LOCK_WAIT))
    except OSError as exc:
        print(f"ai_tokens: state dir unusable: {short_error(exc)}", file=sys.stderr)
        return 2

    if lock_fd is None:
        # Another run (usually a backfill) holds the lock: report the last saved state.
        ledger, problem = load_ledger(state_dir, readonly=True)
        errors = {"ledger": "another run holds the lock; showing last saved totals"}
        if problem:
            errors["ledger"] += "; " + problem
        emit(build_output(ledger, now, errors, int(ledger.get("pending_files") or 0)), args.pretty)
        return 0

    try:
        ledger, problem = load_ledger(state_dir)
        run = Run(ledger, now, deadline, progress=args.backfill)
        if problem:
            run.error("ledger", problem)
        sync = omp_start_sync(run, env, not args.no_omp_sync)
        for name, step in (("hermes", lambda: hermes_source(run, env)),
                           ("files", lambda: file_sources(run, state_dir, env))):
            try:
                step()
            except Exception as exc:  # one source must never stop the others
                run.error(name, short_error(exc))
        try:
            omp_finish_sync(run, sync)
            omp_source(run, env)
        except Exception as exc:
            run.error("omp", short_error(exc))
        try:
            save(run, state_dir)
        except OSError as exc:
            print(f"ai_tokens: cannot write ledger: {short_error(exc)}", file=sys.stderr)
            return 2
        run.say(f"done in {time.monotonic() - started:.1f}s, pending files {run.pending}")
        emit(build_output(ledger, now, run.errors, run.pending), args.pretty)
        return 0
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
