"""Offline state-machine regressions for the retained Linux desktop worker.

The real execute/monitor paths run against temporary task state and rollout
files. Only the desktop transport, bootstrap, Git inspection and clock are
doubles: no model, user session, socket or installed desktop is contacted.
"""

import argparse
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "claudex-bridge" / "scripts"
worker = None
if sys.platform == "linux":
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("claudex_worker_under_test", SCRIPTS / "worker.py")
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
    finally:
        sys.path.remove(str(SCRIPTS))


@unittest.skipUnless(sys.platform == "linux", "The retained desktop worker is a Linux adapter")
class WorkerTests(unittest.TestCase):
    THREAD_ID = "00000000-0000-4000-8000-000000000001"
    TURN_ID = "original-turn"
    MESSAGE_ID = "original-client-message"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claudex-worker-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "state"
        self.root.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        self.codex_home = self.base / "codex-home"
        (self.codex_home / "sessions").mkdir(parents=True)
        self.rollout = self.codex_home / "sessions" / "rollout.jsonl"
        self.rollout.touch()
        self.prompt = self.base / "prompt.txt"
        self.prompt.write_text("Implement the specified change and report the test evidence.", encoding="utf-8")
        self.notes = self.base / "review.md"
        self.notes.write_text("Reviewed the actual change and test evidence.", encoding="utf-8")

        bridge_module = sys.modules[worker.Desktop.__module__]
        self.patch(bridge_module, "codex_home", return_value=self.codex_home)
        # Fail loudly if an implementation change accidentally escapes the doubles.
        self.patch(bridge_module.socket, "socket", side_effect=AssertionError("Real IPC is forbidden in offline tests"))
        self.patch(worker.subprocess, "Popen", side_effect=AssertionError("Real bootstrap is forbidden in offline tests"))
        self.patch(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b""))
        self.patch(worker, "git_project", return_value=str(self.project))
        self.patch(worker, "verify_installation", return_value=self.base / "unexecuted-codex")
        self.emitted = self.patch(worker, "emit")
        self.desktop = mock.MagicMock(spec=worker.Desktop)
        self.desktop.__enter__.return_value = self.desktop
        self.desktop.__exit__.return_value = False
        self.desktop.attach.return_value = self.state()
        self.desktop_type = self.patch(worker, "Desktop", return_value=self.desktop)
        self.bootstrap = self.patch(worker, "create_thread", side_effect=self.bootstrap_thread)
        ticks = itertools.count(1, 5)

        def monotonic():
            tick = next(ticks)
            if tick > 2000:
                raise AssertionError("Monitor failed to terminate under the bounded test clock")
            return tick

        self.patch(worker.time, "monotonic", side_effect=monotonic)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def args(self, action, task="task", **overrides):
        values = dict(action=action, task=task, project=str(self.project),
                      prompt_file=str(self.prompt), model="test-model", effort="high",
                      notes_file=str(self.notes), decision="pass")
        values.update(overrides)
        return argparse.Namespace(**values)

    def bootstrap_thread(self, binary, project, title, directory, remember, **kwargs):
        remember(self.THREAD_ID, str(self.rollout))

    def state(self, *turns, requests=None):
        return {"id": self.THREAD_ID, "turns": list(turns), "requests": requests or []}

    def turn(self, turn_id=TURN_ID, message_id=MESSAGE_ID, status="inProgress"):
        return {"turnId": turn_id, "status": status,
                "params": {"clientUserMessageId": message_id}}

    def completion(self, turn_id=TURN_ID, text="Verified worker result"):
        event = {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": turn_id, "last_agent_message": text}}
        with self.rollout.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    def metadata(self, task="task"):
        return self.root / task / "task.json"

    def read(self, task="task"):
        return worker.read_json(self.metadata(task))

    def seed(self, task="task", status="detached", unresolved=True,
             turn_id=TURN_ID, completed=False):
        directory = self.root / task
        folder = directory / "attempt-001"
        folder.mkdir(parents=True)
        attempt = {"number": 1, "directory": folder.name, "kind": "start",
                   "client_message_id": self.MESSAGE_ID, "turn_id": turn_id,
                   "turn_completed": completed, "rollout_start": 0,
                   "submission_state": "started" if turn_id else "unknown"}
        data = {"task_id": task, "project": str(self.project), "backend": worker.BACKEND,
                "thread_id": self.THREAD_ID, "rollout_path": str(self.rollout),
                "status": status, "unresolved": unresolved, "created_at": worker.now(),
                "attempts": [attempt]}
        worker.save(self.metadata(task), data)
        return directory, folder

    def uncertain_start(self):
        def lose_acknowledgment(prompt, message_id):
            persisted = self.read()
            self.assertTrue(persisted["unresolved"])
            self.assertEqual(persisted["status"], "submitting")
            self.assertEqual(persisted["attempts"][-1]["submission_state"], "unknown")
            self.assertEqual(persisted["attempts"][-1]["client_message_id"], message_id)
            raise worker.BridgeError("The submission acknowledgment was lost")

        self.desktop.start.side_effect = lose_acknowledgment
        self.assertEqual(worker.execute(self.args("start"), self.root), 1)

    def test_uncertain_submission_is_saved_before_send_and_never_repeated(self):
        self.uncertain_start()
        saved = self.read()
        self.assertEqual(saved["status"], "detached")
        self.assertTrue(saved["unresolved"])

        # A repeated start is a status read, even if the IPC acknowledgment was lost.
        worker.execute(self.args("start"), self.root)
        for args in (self.args("resume"), self.args("start", task="replacement")):
            with self.subTest(action=args.action, task=args.task):
                with self.assertRaisesRegex(RuntimeError, "unresolved execution"):
                    worker.execute(args, self.root)
        self.desktop.start.assert_called_once()
        self.bootstrap.assert_called_once()
        self.assertEqual(len(self.read()["attempts"]), 1)
        self.assertFalse((self.root / "replacement").exists())

    def test_watch_recovers_lost_acknowledgment_by_client_message_id_without_resending(self):
        self.uncertain_start()
        message_id = self.read()["attempts"][-1]["client_message_id"]
        self.completion("other-turn", "Unrelated result must not be delivered")
        self.completion(self.TURN_ID, "The exact original turn completed")
        self.desktop.snapshot.return_value = self.state(
            self.turn("other-turn", "other-message", "completed"),
            self.turn(message_id=message_id, status="completed"))

        self.assertEqual(worker.execute(self.args("watch"), self.root), 0)
        saved = self.read()
        self.assertEqual(saved["status"], "needs_review")
        self.assertFalse(saved["unresolved"])
        self.assertEqual(saved["attempts"][-1]["turn_id"], self.TURN_ID)
        self.assertTrue(saved["attempts"][-1]["turn_completed"])
        self.assertEqual((self.root / "task/attempt-001/result.txt").read_text(),
                         "The exact original turn completed")
        self.desktop.start.assert_called_once()
        self.bootstrap.assert_called_once()
        self.assertEqual(len(saved["attempts"]), 1)

    def test_watch_prefers_saved_turn_id_over_another_matching_client_message(self):
        self.seed()
        self.completion()
        original = self.turn(message_id="message-metadata-changed", status="completed")
        state = self.state(self.turn("newer-turn", self.MESSAGE_ID))
        # Original turns can live in paginated history instead of the live turn list.
        state["turnHistory"] = {"history": {"entitiesByKey": {self.TURN_ID: original}}}
        self.desktop.snapshot.return_value = state

        self.assertEqual(worker.execute(self.args("watch"), self.root), 0)
        self.assertEqual(self.read()["attempts"][-1]["turn_id"], self.TURN_ID)
        self.desktop.start.assert_not_called()
        self.bootstrap.assert_not_called()

    def test_watch_of_a_resolved_attempt_does_not_open_or_submit_anything(self):
        self.seed(status="needs_review", unresolved=False, completed=True)
        before = self.read()
        self.assertEqual(worker.execute(self.args("watch"), self.root), 0)
        self.desktop_type.assert_not_called()
        self.bootstrap.assert_not_called()
        self.assertEqual(self.read(), before)

    def test_disconnect_never_promotes_a_rollout_result_without_desktop_confirmation(self):
        _, folder = self.seed()
        self.completion(text="A raw result alone is insufficient")
        self.desktop.snapshot.return_value = self.state(self.turn())
        self.desktop.pump.side_effect = worker.BridgeError("Desktop IPC disconnected")

        self.assertEqual(worker.execute(self.args("watch"), self.root), 1)
        saved = self.read()
        self.assertEqual(saved["status"], "detached")
        self.assertTrue(saved["unresolved"])
        self.assertFalse(saved["attempts"][-1]["turn_completed"])
        self.assertFalse((folder / "result.txt").exists())
        self.desktop.start.assert_not_called()

    def test_desktop_completed_requires_a_nonempty_result_for_the_exact_turn(self):
        cases = [("other-turn", "Someone else's result"), (self.TURN_ID, ""), (self.TURN_ID, "   ")]
        for index, (completion_turn, text) in enumerate(cases):
            with self.subTest(completion_turn=completion_turn, text=text):
                task = "missing-result-" + str(index)
                self.rollout.write_text("")
                _, folder = self.seed(task=task)
                self.completion(completion_turn, text)
                self.desktop.snapshot.return_value = self.state(self.turn(status="completed"))
                self.assertEqual(worker.execute(self.args("watch", task=task), self.root), 1)
                saved = self.read(task)
                self.assertEqual(saved["status"], "failed")
                self.assertFalse(saved["unresolved"])
                self.assertFalse(saved["attempts"][-1]["turn_completed"])
                self.assertFalse((folder / "result.txt").exists())
        self.desktop.start.assert_not_called()

    def test_stop_reconnects_and_interrupts_only_the_original_turn(self):
        for index, known_id in enumerate((self.TURN_ID, None)):
            with self.subTest(turn_id_known=known_id is not None):
                task = "stop-" + str(index)
                self.seed(task=task, turn_id=known_id)
                self.desktop.reset_mock()
                self.desktop.snapshot.side_effect = [
                    self.state(self.turn(), self.turn("unrelated-active-turn", "other-message")),
                    self.state(self.turn(status="interrupted"),
                               self.turn("unrelated-active-turn", "other-message")),
                ]
                self.assertEqual(worker.stop(self.args("stop", task=task), self.root), 1)
                self.desktop.attach.assert_called_once_with(self.THREAD_ID)
                self.desktop.interrupt.assert_called_once_with(self.TURN_ID)
                self.desktop.start.assert_not_called()
                saved = self.read(task)
                self.assertEqual(saved["status"], "interrupted")
                self.assertFalse(saved["unresolved"])
                self.assertFalse(saved["attempts"][-1]["turn_completed"])
        self.bootstrap.assert_not_called()

    def test_stop_refuses_to_guess_when_a_client_message_matches_multiple_turns(self):
        self.seed(turn_id=None)
        self.desktop.snapshot.return_value = self.state(
            self.turn("candidate-one"), self.turn("candidate-two"))

        self.assertEqual(worker.stop(self.args("stop"), self.root), 1)
        saved = self.read()
        self.assertEqual(saved["status"], "detached")
        self.assertTrue(saved["unresolved"])
        self.assertIsNone(saved["attempts"][-1]["turn_id"])
        self.desktop.interrupt.assert_not_called()
        self.desktop.start.assert_not_called()

    def test_review_pass_rejects_unresolved_or_unverified_states(self):
        for status, unresolved in [("needs_review", True), ("running", True),
                                   ("detached", True), ("failed", False),
                                   ("interrupted", False), ("blocked", False)]:
            with self.subTest(status=status, unresolved=unresolved):
                task = status + ("-unresolved" if unresolved else "")
                directory, _ = self.seed(task=task, status=status, unresolved=unresolved)
                before = self.read(task)
                with self.assertRaises(ValueError):
                    worker.record_review(self.args("review", task=task), self.root)
                self.assertEqual(self.read(task), before)
                self.assertEqual(list(directory.glob("review-*.md")), [])

    def test_review_can_be_revised_for_the_same_verified_delivery(self):
        self.seed(status="needs_review", unresolved=False, completed=True)
        before = self.read()["attempts"]
        self.assertEqual(worker.record_review(self.args("review", decision="revise"), self.root), 0)
        self.assertEqual(self.read()["status"], "revision_requested")
        self.assertEqual(worker.record_review(self.args("review"), self.root), 0)
        self.assertEqual(self.read()["status"], "accepted")
        self.assertEqual(self.read()["attempts"], before)
        self.desktop_type.assert_not_called()

    def test_rework_reuses_thread_and_requires_a_fresh_review(self):
        completed_turns = []

        def complete(prompt, message_id):
            turn_id = "delivery-" + str(len(completed_turns) + 1)
            self.completion(turn_id, "Delivery " + turn_id)
            completed_turns.append(self.turn(turn_id, message_id, "completed"))
            return turn_id

        self.desktop.start.side_effect = complete
        self.desktop.attach.side_effect = lambda _thread: self.state(*completed_turns)
        self.desktop.snapshot.side_effect = lambda: self.state(*completed_turns)
        self.assertEqual(worker.execute(self.args("start"), self.root), 0)
        with self.assertRaisesRegex(ValueError, "must review"):
            worker.execute(self.args("resume"), self.root)
        self.desktop.start.assert_called_once()

        self.assertEqual(worker.record_review(self.args("review", decision="revise"), self.root), 0)
        self.prompt.write_text("Fix the specifically identified acceptance failure.", encoding="utf-8")
        self.assertEqual(worker.execute(self.args("resume"), self.root), 0)
        saved = self.read()
        self.assertEqual(saved["status"], "needs_review")
        self.assertNotIn("review", saved)
        self.assertEqual([attempt["turn_id"] for attempt in saved["attempts"]],
                         ["delivery-1", "delivery-2"])
        self.assertEqual(len({attempt["client_message_id"] for attempt in saved["attempts"]}), 2)
        self.bootstrap.assert_called_once()
        self.assertEqual(self.desktop.attach.call_args_list,
                         [mock.call(self.THREAD_ID), mock.call(self.THREAD_ID)])
        self.assertEqual(self.desktop.start.call_count, 2)

        self.assertEqual(worker.record_review(self.args("review"), self.root), 0)
        saved = self.read()
        self.assertEqual(saved["status"], "accepted")
        self.assertEqual(saved["review"]["attempt"], 2)
        self.assertEqual(Path(saved["review"]["notes_file"]).read_text(), self.notes.read_text())


if __name__ == "__main__":
    unittest.main()
