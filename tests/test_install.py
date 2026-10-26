"""Installation never overwrites user content or carries account/task data."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_install", ROOT / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.paths = installer.destinations("both", home=self.home, env={})
        self.source = self.home / "package"
        (self.source / "scripts").mkdir(parents=True)
        for name in ("SKILL.md", "scripts/bridge.py", "scripts/workflow.py"):
            (self.source / name).write_text("initial\n")

    def install(self):
        return installer.install(self.source, self.paths, {"runtime_ready": True})

    def test_unaccepted_install_creates_nothing(self):
        with self.assertRaises(installer.InstallError):
            installer.install(self.source, self.paths, {"runtime_ready": False})
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_foreign_or_modified_copy_blocks_both_targets(self):
        self.paths[1].mkdir(parents=True)
        (self.paths[1] / "personal.txt").write_text("keep")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertFalse(self.paths[0].exists())
        self.assertEqual((self.paths[1] / "personal.txt").read_text(), "keep")

    def test_update_and_remove_preserve_neighbouring_settings_state_and_old_skill(self):
        preserved = [self.home / ".claude/settings.json", self.home / ".codex/config.toml",
                     self.home / ".claude/skills/codex-worker/SKILL.md",
                     self.home / ".local/state/claude-codex-worker/task.json"]
        for path in preserved:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("user-owned")
        self.install()
        (self.source / "SKILL.md").write_text("updated")
        self.install()
        for path in self.paths:
            self.assertEqual((path / "SKILL.md").read_text(), "updated")
            self.assertTrue(installer.owned(path))
        installer.uninstall(self.paths)
        self.assertTrue(all(not path.exists() for path in self.paths))
        self.assertTrue(all(path.read_text() == "user-owned" for path in preserved))

    def test_user_edits_block_update_and_uninstall_without_partial_removal(self):
        self.install()
        (self.paths[1] / "SKILL.md").write_text("personal edit")
        for action in (self.install, lambda: installer.uninstall(self.paths)):
            with self.assertRaises(installer.InstallError):
                action()
        self.assertEqual((self.paths[0] / "SKILL.md").read_text(), "initial\n")
        self.assertEqual((self.paths[1] / "SKILL.md").read_text(), "personal edit")

    def test_failed_second_activation_rolls_back_both_existing_copies(self):
        self.install()
        (self.source / "SKILL.md").write_text("updated")
        original = Path.rename

        def rename(path, target):
            if path.name.startswith(".claudex-stage-") and target == self.paths[1]:
                raise OSError("simulated activation failure")
            return original(path, target)

        with mock.patch.object(Path, "rename", rename), self.assertRaises(OSError):
            self.install()
        for path in self.paths:
            self.assertEqual((path / "SKILL.md").read_text(), "initial\n")
            self.assertTrue(installer.owned(path))
            self.assertEqual(list(path.parent.glob(".claudex-*")), [])

    def test_linked_package_and_malformed_manifest_are_rejected(self):
        (self.source / "linked").symlink_to(self.home / "account")
        with self.assertRaises(installer.InstallError):
            self.install()
        (self.source / "linked").unlink()
        self.install()
        (self.paths[0] / installer.MANIFEST).write_text("[]")
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.paths)

    def test_explicit_configuration_roots_and_duplicate_targets(self):
        env = {"CLAUDE_CONFIG_DIR": str(self.home / "shared"), "CODEX_HOME": str(self.home / "shared")}
        self.assertEqual(installer.destinations("both", self.home, env),
                         [self.home / "shared/skills/claudex-bridge"])

    def test_changed_executable_mode_is_preserved(self):
        self.install()
        path = self.paths[1] / "scripts/bridge.py"
        path.chmod(0o700)
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_real_package_runs_doctor_outside_repository(self):
        installer.install(installer.SOURCE, self.paths, {"runtime_ready": True})
        for path in self.paths:
            result = subprocess.run([sys.executable, "-B", str(path / "scripts/bridge.py"),
                                     "doctor", "--json"], cwd=self.home, capture_output=True, text=True)
            self.assertIn(result.returncode, (0, 2), result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["mode"], "read_only_no_model_calls")
            self.assertFalse(report["release_ready"])
            self.assertEqual(installer.inventory(path), installer.inventory(installer.SOURCE))


if __name__ == "__main__":
    unittest.main()
