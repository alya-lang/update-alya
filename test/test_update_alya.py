"""Offline unit tests for workspace support in update_alya.py.

Run: python3 -m unittest discover -s test -v (no network access).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update_alya as ua


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


VIRTUAL_ROOT = '[workspace]\nmembers = ["crates/*"]\n'


def member_toml(name, deps=""):
    return (
        f'[package]\nname = "{name}"\nversion = "0.1.0"\nentry = "src/main.alya"\n{deps}'
    )


class WorkspaceHelpersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ua-ws-"))
        write(self.tmp / "alya.toml", VIRTUAL_ROOT)
        write(self.tmp / "crates" / "a" / "alya.toml", member_toml("a"))
        write(self.tmp / "crates" / "b" / "alya.toml", member_toml("b"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_declares_workspace(self):
        self.assertTrue(ua.manifest_declares_workspace(self.tmp / "alya.toml"))
        self.assertFalse(
            ua.manifest_declares_workspace(self.tmp / "crates" / "a" / "alya.toml")
        )
        self.assertFalse(ua.manifest_declares_workspace(self.tmp / "missing.toml"))

    def test_find_root(self):
        self.assertEqual(ua.find_workspace_root(self.tmp / "crates" / "a"), self.tmp)
        self.assertEqual(ua.find_workspace_root(self.tmp), self.tmp)
        self.assertIsNone(ua.find_workspace_root(Path(tempfile.gettempdir())))

    def test_expand_members(self):
        members = ua.expand_workspace_members(self.tmp)
        self.assertEqual(members, [self.tmp / "crates" / "a", self.tmp / "crates" / "b"])

    def test_expand_exclude(self):
        write(
            self.tmp / "alya.toml",
            '[workspace]\nmembers = ["crates/*"]\nexclude = ["crates/b"]\n',
        )
        self.assertEqual(
            ua.expand_workspace_members(self.tmp), [self.tmp / "crates" / "a"]
        )

    def test_expand_missing_manifest(self):
        (self.tmp / "crates" / "c").mkdir(parents=True)
        with self.assertRaises(RuntimeError):
            ua.expand_workspace_members(self.tmp)

    def test_expand_nested_workspace(self):
        write(self.tmp / "crates" / "b" / "alya.toml", VIRTUAL_ROOT)
        with self.assertRaises(RuntimeError):
            ua.expand_workspace_members(self.tmp)

    def test_fallback_refuses_members(self):
        bumps, skipped, new_text = ua.fallback_bump(
            self.tmp / "crates" / "a", "", self.tmp / "crates" / "a" / "alya.toml"
        )
        self.assertEqual(bumps, [])
        self.assertIsNone(new_text)
        self.assertTrue(any("workspace member" in s for s in skipped))

    def test_compiler_dry_run_pins_per_member(self):
        dep = 'x = { git = "https://github.com/o/x", tag = "v1.0.0" }\n'
        write(
            self.tmp / "crates" / "a" / "alya.toml",
            member_toml("a", "\n[dependencies]\n" + dep),
        )
        manifests = [
            self.tmp / "crates" / "a" / "alya.toml",
            self.tmp / "crates" / "b" / "alya.toml",
        ]
        with mock.patch.object(
            ua, "latest_release_tag", return_value="v1.2.0"
        ), mock.patch.object(ua, "branch_head_sha", return_value=None):
            bumps, skipped, _ = ua.compiler_bump(
                self.tmp, "", True, manifests, self.tmp, str(self.tmp)
            )
        self.assertEqual(len(bumps), 1)
        self.assertEqual(bumps[0]["dir"], str(self.tmp / "crates" / "a"))
        self.assertEqual(bumps[0]["wsroot"], str(self.tmp))


if __name__ == "__main__":
    unittest.main()
