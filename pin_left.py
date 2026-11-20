#!/usr/bin/env python3
"""Put the two token widgets on bar row 1, right after the workspaces.

Usage: pin_left.py [--shell-json PATH] [--dry-run]

Safe to repeat. It makes a dated backup before the first change, writes the file
atomically, and leaves every other bar entry where it is. Omarchy reloads
shell.json by itself in about 6 to 8 seconds.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

WIDGETS = ["meshvault.ai-tokens", "meshvault.cluster-tokens"]
AFTER = ["omarchy.menu", "omarchy.workspaces"]


def plan(layout):
    left = layout.get("left", [])
    by_id = {entry["id"]: entry for entry in left}
    missing = [w for w in WIDGETS if w not in by_id]
    if missing:
        raise SystemExit("not in the left section yet (run: omarchy plugin enable <id> left): " + ", ".join(missing))
    for wid in WIDGETS:
        by_id[wid]["barRow"] = 1
    head = [by_id[i] for i in AFTER if i in by_id]
    mine = [by_id[w] for w in WIDGETS]
    taken = {e["id"] for e in head + mine}
    return head + mine + [e for e in left if e["id"] not in taken]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shell-json", default=str(Path.home() / ".config/omarchy/shell.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = Path(args.shell_json)
    config = json.loads(path.read_text(encoding="utf-8"))
    new_left = plan(config["bar"]["layout"])
    before = [(e["id"], e.get("barRow")) for e in config["bar"]["layout"]["left"]]
    config["bar"]["layout"]["left"] = new_left
    after = [(e["id"], e.get("barRow")) for e in new_left]
    print("left:", after)
    if args.dry_run:
        return 0
    stamp = dt.datetime.now().strftime("%Y%m%d")
    backup = path.with_name(path.name + ".bak-" + stamp + "-tokenbar-pin")
    if before != after and not backup.exists():
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".shell.json.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
