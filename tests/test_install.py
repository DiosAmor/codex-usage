import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import storage

spec = importlib.util.spec_from_file_location("monitor_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.file_patch = mock.patch("storage.__file__", str(self.home / "source/storage.py"))
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.temp.cleanup()

    def test_default_outside_install_and_platform_paths(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("storage.Path.home", return_value=self.home):
            with mock.patch("storage.sys.platform", "linux"):
                self.assertEqual(storage.default_data_dir(), self.home / ".local/state/codex-usage")
            with mock.patch("storage.sys.platform", "darwin"):
                self.assertEqual(storage.default_data_dir(), self.home / "Library/Application Support/codex-usage")

    def test_explicit_override_xdg_and_legacy(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / "state")}, clear=True):
            self.assertEqual(storage.default_data_dir(), self.home / "state/codex-usage")
            legacy = self.home / "source/.private"
            legacy.mkdir(parents=True)
            (legacy / "accounts.json").write_text('{"accounts": []}')
            self.assertEqual(storage.default_data_dir(), legacy)
            with mock.patch.dict(os.environ, {"CODEX_USAGE_DATA_DIR": str(self.home / "custom")}):
                self.assertEqual(storage.default_data_dir(), self.home / "custom")


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prefix = self.root / "prefix with spaces"

    def tearDown(self):
        self.temp.cleanup()

    def test_installed_demo_and_help_work_outside_checkout(self):
        command = installer.install(self.prefix)
        for args in (["--help"], ["--version"], ["--demo", "--json"]):
            result = subprocess.run([str(command), *args], cwd=self.root,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if "--json" in args:
                self.assertTrue(json.loads(result.stdout)["demo"])
            if "--version" in args:
                self.assertIn("0.3.0", result.stdout)
        installed = self.prefix / "share/codex-usage"
        self.assertEqual(sorted(p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
                         sorted(installer.RUNTIME_FILES))
        self.assertFalse((installed / ".private").exists())
        self.assertFalse((installed / ".git").exists())
        self.assertFalse((installed / "tests").exists())

    def test_local_account_state_survives_reinstallation(self):
        command = installer.install(self.prefix)
        external = self.root / "state"
        sources = self.root / "empty-codex"
        sources.mkdir()
        env = dict(os.environ, CODEX_USAGE_DATA_DIR=str(external), CODEX_HOME=str(sources))
        result = subprocess.run([str(command), "list", "--json"], env=env, cwd=self.root,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["accounts"], [])
        self.assertTrue((external / "accounts").is_dir())
        sentinel = external / "accounts.json"
        sentinel.write_text('{"version":1,"ignored":[],"accounts":[]}')
        legacy = self.prefix / "share/codex-usage/.private/auth.json"
        legacy.parent.mkdir()
        legacy.write_bytes(b"TEST_ONLY_PRIVATE_DATA")
        before = (sentinel.read_bytes(), legacy.read_bytes())
        installer.install(self.prefix)
        self.assertEqual((sentinel.read_bytes(), legacy.read_bytes()), before)

    def test_other_existing_command_not_overwritten(self):
        command = self.prefix / "bin/codex-usage"
        command.parent.mkdir(parents=True)
        command.write_text("unrelated tool")
        with self.assertRaises(ValueError):
            installer.install(self.prefix)
        self.assertEqual(command.read_text(), "unrelated tool")

    def test_installer_rejects_launcher_symlink(self):
        command = self.prefix / "bin/codex-usage"
        command.parent.mkdir(parents=True)
        destination = self.root / "other"
        destination.write_text("keep")
        command.symlink_to(destination)
        with self.assertRaises(ValueError):
            installer.install(self.prefix)
        self.assertEqual(destination.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
