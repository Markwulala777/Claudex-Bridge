"""The native view gets its own VS Code profile and cannot silently change sessions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))
import native_view


@unittest.skipIf(sys.platform == "win32", "The executable wrapper launcher currently targets Unix")
class NativeViewTests(unittest.TestCase):
    SESSION = "00000000-0000-4000-8000-000000000001"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project $(not-a-command) 'quoted'"
        self.project.mkdir()
        self.extension = self.root / "anthropic.claude-code-2.1.295-linux-x64"
        self.extension.mkdir()
        (self.extension / "package.json").write_text(json.dumps({
            "publisher": "Anthropic", "name": "claude-code", "version": "2.1.295",
        }), encoding="utf-8")
        self.binding = self.root / "binding.json"
        self.source = self.root / "owner.jsonl"
        self.source.write_text(json.dumps({
            "type": "user", "sessionId": self.SESSION, "version": "2.1.295",
            "uuid": "00000000-0000-4000-8000-000000000011",
            "message": {"role": "user", "content": "Test history"},
        }) + "\n", encoding="utf-8")
        self.binding.write_text(json.dumps({
            "schema": 1, "session_id": self.SESSION, "source": str(self.source),
            "source_format": "transcript", "claude_code_version": "2.1.295",
            "history": "native", "start_offset": self.source.stat().st_size,
        }), encoding="utf-8")
        self.directory = self.root / "isolated view"

    def prepare(self):
        with mock.patch.object(native_view.shutil, "which", return_value="/observed/code"):
            return native_view.prepare(self.directory, self.project, self.binding, self.extension)

    def test_prepare_only_writes_its_own_profile_and_does_not_launch_code(self):
        with mock.patch.object(native_view.subprocess, "run", side_effect=AssertionError("No process expected")):
            result = self.prepare()
        self.assertFalse(result["execution_started"])
        self.assertFalse(result["native_ui_verified"])
        settings = json.loads((self.directory / "profile/User/settings.json").read_text())
        self.assertEqual(settings["claudeCode.claudeProcessWrapper"], str(self.directory / "claude-view-wrapper"))
        self.assertFalse(settings["extensions.autoUpdate"])
        self.assertTrue(settings["claudeCode.disableLoginPrompt"])
        self.assertEqual(list(self.project.iterdir()), [])
        launcher = self.directory / "claude-view-wrapper"
        compile(launcher.read_text(), str(launcher), "exec")
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.directory / "extensions" / self.extension.name).is_symlink())

    def test_existing_directory_is_preserved(self):
        self.directory.mkdir()
        keep = self.directory / "user.txt"
        keep.write_text("Keep existing work")
        with self.assertRaisesRegex(native_view.ViewError, "already exists"):
            self.prepare()
        self.assertEqual(keep.read_text(), "Keep existing work")
        self.assertEqual(list(self.directory.iterdir()), [keep])

    def test_binding_cannot_be_swapped_after_preparation(self):
        self.prepare()
        updated = json.loads(self.binding.read_text())
        updated["session_id"] = "00000000-0000-4000-8000-000000000002"
        self.binding.write_text(json.dumps(updated))
        with mock.patch.object(native_view.subprocess, "run") as run:
            with self.assertRaisesRegex(native_view.ViewError, "Binding changed"):
                native_view.open_view(self.directory)
        run.assert_not_called()

    def test_open_uses_exact_profile_project_and_session_without_shell(self):
        self.prepare()
        with mock.patch.object(native_view.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = native_view.open_view(self.directory)
        self.assertEqual(run.call_count, 2)
        first, second = run.call_args_list
        self.assertEqual(first.args[0][-2:], ["--new-window", str(self.project)])
        self.assertIn(str(self.directory / "profile"), first.args[0])
        self.assertEqual(second.args[0][-1], "vscode://anthropic.claude-code/open?session=" + self.SESSION)
        self.assertFalse(first.kwargs.get("shell", False))
        self.assertFalse(result["native_ui_verified"])

    def test_failed_window_launch_does_not_attempt_uri_or_claim_opened(self):
        self.prepare()
        with mock.patch.object(native_view.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "launch failed")) as run:
            with self.assertRaisesRegex(native_view.ViewError, "could not open"):
                native_view.open_view(self.directory)
        run.assert_called_once()

    def test_unproven_source_creates_no_profile(self):
        self.source.write_text("")
        with self.assertRaises(native_view.ViewError):
            self.prepare()
        self.assertFalse(self.directory.exists())

    def test_timeout_does_not_send_uri_or_claim_opened(self):
        self.prepare()
        with mock.patch.object(native_view.subprocess, "run", side_effect=subprocess.TimeoutExpired("code", 30)) as run:
            with self.assertRaisesRegex(native_view.ViewError, "timed out"):
                native_view.open_view(self.directory)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
