"""Behavior tests for pin_left.py: bar order, barRow and the backup rule."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pin_left  # noqa: E402

WIDGETS = ["meshvault.ai-tokens", "meshvault.cluster-tokens"]


class PinLeftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "shell.json"

    def write(self, *entries):
        self.path.write_text(json.dumps({"bar": {"layout": {"left": list(entries)}}}), encoding="utf-8")

    def run_main(self, *extra):
        argv = sys.argv
        sys.argv = ["pin_left.py", "--shell-json", str(self.path), *extra]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return pin_left.main()
        finally:
            sys.argv = argv

    def left(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["bar"]["layout"]["left"]

    def backups(self):
        return sorted(p.name for p in self.dir.glob("shell.json.bak-*"))

    def test_order_is_menu_workspaces_widgets_then_the_rest(self):
        self.write({"id": "omarchy.tray"}, {"id": "meshvault.cluster-tokens"},
                   {"id": "omarchy.workspaces"}, {"id": "meshvault.ai-tokens"}, {"id": "omarchy.menu"})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual([e["id"] for e in self.left()],
                         ["omarchy.menu", "omarchy.workspaces"] + WIDGETS + ["omarchy.tray"])
        self.assertEqual([e["barRow"] for e in self.left()[2:4]], [1, 1])

    def test_backup_is_made_when_only_bar_row_changes(self):
        # Order is already right, so only barRow changes. The file must still be
        # backed up first: the script promises a dated copy before any change.
        self.write({"id": "omarchy.menu"}, {"id": "omarchy.workspaces"},
                   {"id": "meshvault.ai-tokens"}, {"id": "meshvault.cluster-tokens"})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual([e["barRow"] for e in self.left()[2:4]], [1, 1])

    def test_no_backup_when_nothing_changes(self):
        self.write({"id": "omarchy.menu"}, {"id": "omarchy.workspaces"},
                   {"id": "meshvault.ai-tokens", "barRow": 1},
                   {"id": "meshvault.cluster-tokens", "barRow": 1})
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.backups(), [])

    def test_missing_widget_exits_and_writes_nothing(self):
        self.write({"id": "omarchy.menu"}, {"id": "meshvault.ai-tokens"})
        before = self.path.read_bytes()
        with self.assertRaises(SystemExit) as raised:
            self.run_main()
        self.assertIn("meshvault.cluster-tokens", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_dry_run_writes_nothing(self):
        self.write({"id": "omarchy.tray"}, {"id": "meshvault.ai-tokens"},
                   {"id": "meshvault.cluster-tokens"})
        before = self.path.read_bytes()
        self.assertEqual(self.run_main("--dry-run"), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.backups(), [])


if __name__ == "__main__":
    unittest.main()
