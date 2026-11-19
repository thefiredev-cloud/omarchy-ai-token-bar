# Build guide: omarchy-token-bar

## Requirements

- An Omarchy shell with the plugin commands (`omarchy plugin`, `omarchy-shell`).
- Python 3 on `PATH`. The scripts use only the standard library.
- Omarchy 4.0.4 for the `WidgetButton` API.

## 1. Run the tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use temporary directories and fake logs. The run takes a few seconds.

## 2. Install

```bash
./install.sh
```

`install.sh` does five things:

1. Copies each plugin folder into `~/.config/omarchy/plugins/<id>`.
2. Runs `omarchy plugin validate` on each copy.
3. Asks the shell to rescan plugins (`omarchy-shell shell rescanPlugins`).
4. Enables any plugin that shows as disabled, in the left section (`omarchy plugin enable <id> left`).
5. Runs `pin_left.py`, which edits `~/.config/omarchy/shell.json`.

`pin_left.py` puts `omarchy.menu` and `omarchy.workspaces` first in the left section if they are there, then the two widgets, then every other entry in its old order. It sets `"barRow": 1` on both widgets. Stock Omarchy 4.0.4 has no `barRow` handling, so on a stock bar only the order changes. If either widget is missing from the left section, the script exits with an error and changes nothing.

Running it again is safe. When the order changes, the script first copies `shell.json` to `shell.json.bak-<YYYYMMDD>-tokenbar-pin` (one backup per day). To preview without writing:

```bash
python3 pin_left.py --dry-run
```

Use `--shell-json <path>` for another config file. Omarchy reloads `shell.json` by itself, so the bar updates in a few seconds.

The first AI Tokens run with a lot of history stops after its 20 second budget and carries on at the next tick. To finish the whole backfill at once, run the script with `--backfill`.

## Data sources

### AI Tokens (`ai_tokens.py`)

| Source | Provider | Default location | Override |
| --- | --- | --- | --- |
| Claude Code logs | Anthropic | `~/.claude/projects` (every `*.jsonl`) | `CLAUDE_PROJECTS_DIR` |
| Codex CLI sessions | Codex | `~/.codex/sessions` (`rollout-*.jsonl`) | `CODEX_SESSIONS_DIR` |
| Grok CLI sessions | Grok | `~/.grok/sessions` (`*/*/usage.json`) | `GROK_SESSIONS_DIR` |
| Hermes SQLite database | Anthropic, Codex, Grok | `~/.hermes/state.db` | `HERMES_STATE_DB` |
| OMP stats database | Anthropic, Codex, Grok | `~/.omp/stats.db` | `OMP_STATS_DB` |

Databases open read-only. A missing source shows under "Unavailable" in the hover text and does not stop the others.

- Claude Code: assistant messages with usage, de-duplicated by message id and request id. Synthetic and all-zero entries are skipped.
- Codex CLI: the running `token_count` totals, turned into per-event deltas. Cached input is subtracted from input and counted as cache reads.
- Grok CLI: each turn in `usage.json`, counted once per session and turn number.
- Hermes: sessions billed through Anthropic, OpenAI Codex or xAI. Hermes keeps one running total per session, so the first sighting is dated to the session end and later growth to the day it is seen.
- OMP: the `messages` table, regrouped by local day on every run. The script runs `omp stats --summary` at most every 5 minutes to refresh that database. Skip it with `--no-omp-sync`, or point `OMP_BIN` at another binary.

### Cluster Tokens (`cluster_tokens.py`)

The script reads `*.jsonl` files from the top level of the telemetry directory and adds the per-sample changes into a per-day ledger. This repo does not include the producer. Your own job writes one JSON object per line with these fields:

- `ts`: ISO 8601 timestamp. Lines not newer than the previous line in the same file are ignored.
- `d_prompt_tokens`, `d_generation_tokens`, `d_requests_total`: the change since the previous sample. If `d_requests_total` is blank, `completed` is used. With neither, the period `requests` is `null` and the hover text says "requests n/a".
- `engine_up`, `served_model`, `max_model_len`, `running`, `waiting`, `kv_usage`, `decode_tps_per_req`, `error`: optional status shown in the hover text.

An optional `last.json` with `raw.created` (epoch seconds) in the same folder gives the engine uptime. The dot reads as down when the newest sample says so or is more than 180 seconds old.

## Configuration

Both scripts keep their ledger in `~/.local/state/meshvault-tokens/`, created with mode 0700 (files 0600). Set `MESHVAULT_TOKENS_STATE` or pass `--state-dir` to move it.

| Setting | Applies to | Meaning |
| --- | --- | --- |
| `--state-dir <path>` | both | Ledger folder |
| `--pretty` | both | Indented JSON |
| `--now <epoch>` | both | Override the clock (used by the tests) |
| `--budget <seconds>` | AI Tokens | Time limit per run. Default 20. `0` means no limit |
| `--backfill` | AI Tokens | Parse all history, print progress to stderr |
| `--no-omp-sync` | AI Tokens | Do not run `omp stats --summary` |
| `--telemetry-dir <path>` | Cluster Tokens | Telemetry folder. Default `~/ops/state/cluster-telemetry`, or `CLUSTER_TELEMETRY_DIR` |

A widget dims its numbers and shows "Data is old or the last read failed" when the last good read is more than 5 minutes old.

## Check the live widgets

```bash
omarchy-shell meshvault.ai-tokens state
omarchy-shell meshvault.cluster-tokens state
```

Both widgets also accept `refresh` and `cycle` through `omarchy-shell <plugin id>`.

## Remove

```bash
omarchy plugin remove meshvault.ai-tokens --yes
omarchy plugin remove meshvault.cluster-tokens --yes
```

Omarchy moves each removed folder to a hidden backup in `~/.config/omarchy/plugins/`. Ledgers and `shell.json` backups stay until you delete them.

## Layout

```text
install.sh                       copy, validate, enable and pin both widgets
pin_left.py                      edit bar order in shell.json
plugins/meshvault.ai-tokens/     BarWidget.qml  ai_tokens.py  manifest.json
plugins/meshvault.cluster-tokens/  BarWidget.qml  cluster_tokens.py  manifest.json
tests/                           test_ai_tokens.py  test_cluster_tokens.py  test_pin_left.py
docs/architecture.svg            architecture diagram
```
