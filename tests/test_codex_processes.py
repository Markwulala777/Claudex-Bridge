"""Exercise attribution and PID reuse protection without any native/model calls."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"))
import codex_processes as processes


@unittest.skipUnless(sys.platform == "linux" and hasattr(os, "pidfd_open"), "Linux pidfds")
class ProcessCancellationTests(unittest.TestCase):
    def child(self, session):
        env = dict(os.environ, CODEX_THREAD_ID=session, CODEX_SESSION_ID=session)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        self.addCleanup(cleanup)
        return child

    def test_only_new_exact_session_commands_are_terminated(self):
        session = str(uuid.uuid4())
        previous = self.child(session)
        before = processes.baseline()
        unrelated = self.child(str(uuid.uuid4()))
        target = self.child(session)
        found = processes.candidates(session, before)
        self.assertEqual([p["pid"] for p in found], [target.pid])
        with processes.PinnedProcesses(found) as commands:
            result = commands.terminate()
        self.assertTrue(result["complete"])
        self.assertEqual(target.wait(timeout=5), -signal.SIGTERM)
        self.assertIsNone(previous.poll())
        self.assertIsNone(unrelated.poll())

    def test_reused_pid_is_never_signalled(self):
        target = {"pid": 123, "start_ticks": 100, "parent": 1}
        with mock.patch.object(os, "pidfd_open", return_value=9), \
                mock.patch.object(os, "close") as close, \
                mock.patch.object(processes, "_process", return_value={**target, "start_ticks": 101}), \
                mock.patch.object(signal, "pidfd_send_signal") as send:
            with processes.PinnedProcesses([target]) as commands:
                self.assertTrue(commands.terminate()["complete"])
            send.assert_not_called()
            close.assert_called_once_with(9)

    def test_missing_or_old_boot_baseline_is_rejected(self):
        for before in (None, {"boot_id": "another-boot", "processes": []}):
            with self.subTest(before=before), self.assertRaises(processes.ProcessError):
                processes.candidates(str(uuid.uuid4()), before)

    def test_children_with_cleared_environment_are_attributed_but_old_processes_are_not(self):
        current = {n: {"pid": n, "start_ticks": 10, "parent": parent}
                   for n, parent in ((1, 0), (2, 1), (3, 2), (4, 3), (5, 0))}
        with mock.patch.object(processes, "_boot", return_value="boot"), \
                mock.patch.object(processes, "_processes", return_value=current), \
                mock.patch.object(processes, "_matches", side_effect=lambda pid, *_: pid in (1, 2)):
            found = processes.candidates("session", {"boot_id": "boot", "processes": [[1, 10]]})
        self.assertEqual([p["pid"] for p in found], [2, 3, 4])

    def test_signal_failure_closes_all_pinned_handles(self):
        target = {"pid": 123, "start_ticks": 100, "parent": 1}
        with mock.patch.object(os, "pidfd_open", return_value=9), \
                mock.patch.object(os, "close") as close, \
                mock.patch.object(processes, "_process", return_value=target), \
                mock.patch.object(signal, "pidfd_send_signal", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                with processes.PinnedProcesses([target]) as commands:
                    commands.terminate()
            close.assert_called_once_with(9)


if __name__ == "__main__":
    unittest.main()
