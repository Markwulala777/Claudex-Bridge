"""Offline state invariants using temporary files, never user state or models."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "claudex-bridge" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import task_state

if sys.platform == "linux":
    import fcntl


@unittest.skipUnless(sys.platform == "linux", "Shared execution lock is validated on Linux")
class TaskStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claudex-task-state-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.root = self.base / "state"
        self.store = task_state.TaskStore(self.root)
        self.initiator = {"product": "claude", "session_id": "claude-main"}
        self.executor = {"product": "codex", "session_id": None}
        self.next_evidence = 0
        self.create()

    def create(self, task="task", **overrides):
        values = dict(project=self.project, initiator=self.initiator,
                      executor=self.executor, requirements="Implement the specified change.")
        values.update(overrides)
        return self.store.create_task(task, **values)

    def read(self):
        return self.store.read("task")

    def begin(self):
        return self.store.begin_attempt("task", actor=self.initiator)

    def bind(self, data=None):
        data = data or self.begin()
        attempt = data["attempts"][-1]
        return self.store.bind_execution("task", attempt["number"], "executor-session",
                                         "turn-%s" % attempt["number"], "turn",
                                         client_message_id=attempt["client_message_id"])

    def evidence(self, raw=b"Saved evidence\n", **metadata):
        self.next_evidence += 1
        path = self.base / ("evidence-%d.json" % self.next_evidence)
        path.write_bytes(raw)
        return task_state.evidence_ref(path, **metadata)

    def terminal_reference(self, data, status="completed", **overrides):
        identity = data["attempts"][-1]["execution"]
        metadata = dict(identity, status=status, event_type="task_complete")
        metadata.update(overrides)
        return self.evidence(json.dumps(metadata).encode(), **metadata)

    def terminal(self, data=None, status="completed", **overrides):
        data = data or self.bind()
        attempt = data["attempts"][-1]
        reference = self.terminal_reference(data, status, **overrides)
        return self.store.observe_terminal("task", attempt["number"], **attempt["execution"],
                                           status=status, evidence=reference)

    def delivery(self, data=None, paths=None, snapshot_hash="a" * 64):
        data = data or self.terminal()
        return self.store.record_delivery("task", data["attempts"][-1]["number"], snapshot_hash,
                                          ["src/a.py", "removed.txt"] if paths is None else paths,
                                          self.evidence(b"Final Git evidence\n"))

    def review_args(self, data):
        attempt = data["attempts"][-1]
        return dict(actor=self.initiator, decision="pass", attempt_number=attempt["number"],
                    requirements_hash=data["requirements"]["hash"],
                    snapshot_hash=attempt["delivery"]["snapshot_hash"],
                    reviewed_paths=attempt["delivery"]["changed_paths"],
                    test_evidence=[self.evidence(b"Tests passed\n", command="python3 -m unittest", exit_code=0)],
                    notes="Inspected the final changes and the attached test output.")

    def accept(self, data=None):
        data = data or self.delivery()
        return self.store.record_review("task", **self.review_args(data))

    def test_state_root_preserves_legacy_environment_precedence(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "xdg"),
                                          "CODEX_WORKER_STATE_DIR": str(self.base / "override")}):
            self.assertEqual(task_state.state_root(), self.base / "override")
            del os.environ["CODEX_WORKER_STATE_DIR"]
            self.assertEqual(task_state.state_root(), self.base / "xdg/claude-codex-worker")

    def test_shared_lock_contends_with_legacy_flock_and_keeps_the_same_inode(self):
        lock = self.root / "worker.lock"
        inode = lock.stat().st_ino
        # The separately installed legacy runner uses a+ and an exclusive,
        # nonblocking flock on this same path. Exercise that OS-level contract
        # without loading user-installed code or bundling the retired runner.
        with lock.open("a+", encoding="utf-8") as legacy_lock:
            with self.store.locked({"task_id": "task"}):
                self.store.bind_executor("task", "executor-session")
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "worker.lock"):
                    task_state.TaskStore(self.root).bind_executor("task", "executor-session")
            fcntl.flock(legacy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.assertRaisesRegex(RuntimeError, "worker.lock"):
                    self.store.bind_executor("task", "executor-session")
            finally:
                fcntl.flock(legacy_lock, fcntl.LOCK_UN)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_begin_is_fsynced_unknown_before_a_send_and_survives_a_process_restart(self):
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(descriptor):
            events.append(("fsync", "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"))
            return real_fsync(descriptor)

        def replace(source, target):
            events.append(("replace", Path(target).name))
            return real_replace(source, target)

        with mock.patch.object(task_state.os, "fsync", side_effect=fsync), \
                mock.patch.object(task_state.os, "replace", side_effect=replace):
            saved = self.begin()
        replaced = events.index(("replace", "task.json"))
        self.assertEqual(events[replaced - 1], ("fsync", "file"))
        self.assertEqual(events[replaced + 1], ("fsync", "directory"))
        self.assertNotIn(("replace", "worker.lock"), events)
        self.assertTrue(saved["unresolved"])
        self.assertEqual(saved["status"], "submitting")
        self.assertEqual(saved["attempts"][-1]["submission_state"], "unknown")
        self.assertTrue(saved["attempts"][-1]["client_message_id"])
        # Simulate a crash before send: no transport is called, and a fresh store
        # must retain uncertainty rather than assuming nothing was submitted.
        restarted = task_state.TaskStore(self.root)
        before = restarted.read("task")
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            restarted.begin_attempt("task", actor=self.initiator)
        self.create("replacement")
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            restarted.begin_attempt("replacement", actor=self.initiator)
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            task_state.assert_no_unresolved(self.root)
        self.assertEqual(restarted.read("task"), before)

    def test_legacy_unresolved_and_running_records_block_new_attempts(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        for status, unresolved in (("detached", True), ("running", False)):
            with self.subTest(status=status):
                (legacy / "task.json").write_text(json.dumps({"task_id": "legacy", "status": status,
                                                             "unresolved": unresolved, "attempts": []}))
                with self.assertRaisesRegex(RuntimeError, "legacy.*unresolved"):
                    self.begin()
                self.assertEqual(self.read()["attempts"], [])
        with self.assertRaisesRegex(task_state.StateError, "Legacy"):
            self.store.read("legacy")

    def test_corrupt_other_state_fails_closed_before_submission(self):
        legacy = self.root / "corrupt"
        legacy.mkdir()
        (legacy / "task.json").write_text("{broken")
        with self.assertRaises(ValueError):
            self.begin()
        self.assertEqual(self.read()["attempts"], [])

    def test_main_roles_project_and_bound_executor_session_cannot_change(self):
        other_project = self.base / "other"
        other_project.mkdir()
        original = self.read()
        for changes in ({"project": other_project},
                        {"initiator": {"product": "claude", "session_id": "another-initiator"}},
                        {"initiator": {"product": "codex", "session_id": "other"},
                         "executor": {"product": "claude", "session_id": None}},
                        {"requirements": "Different task disguised as the same ID"}):
            with self.subTest(changes=changes), self.assertRaises(task_state.StateError):
                self.create(**changes)
        self.assertEqual(self.read(), original)
        bound = self.store.bind_executor("task", "executor-session")
        self.assertEqual(self.store.bind_executor("task", "executor-session"), bound)
        with self.assertRaisesRegex(task_state.StateError, "fixed"):
            self.store.bind_executor("task", "replacement-session")

    def test_only_explicit_initiator_can_dispatch_or_change_requirements(self):
        for actor in ({"product": "codex", "session_id": "executor-session"},
                      {"product": "claude", "session_id": "different-claude"}):
            with self.subTest(actor=actor):
                with self.assertRaisesRegex(task_state.StateError, "initiating"):
                    self.store.begin_attempt("task", actor=actor)
                with self.assertRaisesRegex(task_state.StateError, "initiating"):
                    self.store.update_requirements("task", "Changed", actor=actor)

    def test_unknown_recovery_requires_a_unique_exact_match_and_never_adds_attempts(self):
        data = self.begin()
        attempt = data["attempts"][-1]
        candidate = dict(client_message_id=attempt["client_message_id"], session_id="executor-session",
                         execution_id="original", id_kind="turn")
        for candidates in ([], [dict(candidate, client_message_id="unrelated")],
                           [candidate, dict(candidate, execution_id="ambiguous")]):
            with self.subTest(candidates=candidates), self.assertRaisesRegex(task_state.StateError, "exactly one"):
                self.store.recover_execution("task", 1, candidates)
            self.assertEqual(self.read(), data)
        recovered = self.store.recover_execution("task", 1, [candidate])
        self.assertEqual(len(recovered["attempts"]), 1)
        self.assertEqual(recovered["attempts"][0]["execution"]["execution_id"], "original")
        with self.assertRaisesRegex(task_state.StateError, "cannot be replaced"):
            self.store.recover_execution("task", 1, [dict(candidate, execution_id="later-turn")])

    def test_binding_rejects_wrong_client_message_session_and_id(self):
        data = self.begin()
        client = data["attempts"][0]["client_message_id"]
        self.store.bind_executor("task", "executor-session")
        for session, message in (("other-session", client), ("executor-session", "other-message")):
            with self.subTest(session=session, message=message), self.assertRaises(task_state.StateError):
                self.store.bind_execution("task", 1, session, "turn-1", "turn", client_message_id=message)
        bound = self.bind(data)
        self.assertEqual(self.store.bind_execution("task", 1, "executor-session", "turn-1", "turn",
                                                   client_message_id=client), bound)
        with self.assertRaises(task_state.StateError):
            self.store.bind_execution("task", 1, "executor-session", "other", "turn", client_message_id=client)

    def test_eof_disconnect_wrong_turn_and_missing_evidence_never_complete(self):
        data = self.bind()
        attempt = data["attempts"][-1]
        original = self.read()
        reference = self.terminal_reference(data)
        cases = [dict(status="eof", evidence=reference),
                 dict(status="completed", evidence=dict(reference, event_type="eof")),
                 dict(status="completed", evidence=dict(reference, execution_id="other")),
                 dict(status="completed", evidence=dict(reference, path=str(self.base / "missing"))),
                 dict(status="completed", evidence=dict(reference, sha256="0" * 64))]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises((task_state.StateError, OSError)):
                self.store.observe_terminal("task", 1, **attempt["execution"], **changes)
            self.assertEqual(self.read(), original)
        with self.assertRaisesRegex(task_state.StateError, "exact bound"):
            self.store.observe_terminal("task", 1, "executor-session", "unrelated", "turn",
                                         status="completed", evidence=reference)
        detached = self.store.mark_detached("task", 1, "Transport reached EOF")
        self.assertTrue(detached["unresolved"])
        self.assertFalse(detached["attempts"][0]["turn_completed"])

    def test_terminal_completion_is_idempotent_but_cannot_be_replaced(self):
        data = self.terminal()
        attempt = data["attempts"][0]
        self.assertEqual(data["status"], "needs_review")
        self.assertFalse(data["unresolved"])
        self.assertEqual(self.store.observe_terminal("task", 1, **attempt["execution"], status="completed",
                                                     evidence=attempt["completion_evidence"]), data)
        with self.assertRaisesRegex(task_state.StateError, "cannot be replaced"):
            self.store.observe_terminal("task", 1, **attempt["execution"], status="failed",
                                         evidence=self.terminal_reference(data, "failed"))

    def test_failure_and_abort_resolve_the_submission_but_never_become_delivery(self):
        for status in ("failed", "interrupted", "aborted"):
            with self.subTest(status=status):
                data = self.bind()
                result = self.terminal(data, status, event_type="codex_desktop_terminal")
                self.assertFalse(result["unresolved"])
                self.assertFalse(result["attempts"][-1]["turn_completed"])
                self.assertEqual(result["attempts"][-1]["terminal_status"], status)
                with self.assertRaisesRegex(task_state.StateError, "verified successful"):
                    self.delivery(result)
        self.assertEqual(len({item["client_message_id"] for item in self.read()["attempts"]}), 3)

    def test_reverse_direction_preserves_claude_terminal_error_details(self):
        owner = {"product": "codex", "session_id": "codex-main"}
        self.create("reverse", initiator=owner, executor={"product": "claude", "session_id": "claude-executor"})
        data = self.store.begin_attempt("reverse", actor=owner)
        client = data["attempts"][0]["client_message_id"]
        self.store.bind_execution("reverse", 1, "claude-executor", "request-1", "message", client_message_id=client)
        identity = dict(session_id="claude-executor", execution_id="request-1", id_kind="message")
        invalid = self.evidence(**identity, event_type="turn.complete", status="completed", reason="error", error="quota")
        with self.assertRaisesRegex(task_state.StateError, "error or aborted"):
            self.store.observe_terminal("reverse", 1, **identity, status="completed", evidence=invalid)
        reference = self.evidence(**identity, event_type="turn.complete", status="failed", reason="error", error="quota")
        result = self.store.observe_terminal("reverse", 1, **identity, status="failed", evidence=reference)
        self.assertEqual(result["direction"], "codex-to-claude")
        self.assertEqual(result["attempts"][0]["completion_evidence"]["error"], "quota")
        self.assertFalse(result["unresolved"])

    def test_pass_rejects_executor_wrong_requirements_snapshot_partial_paths_and_failed_tests(self):
        data = self.delivery()
        args = self.review_args(data)
        changes = [{"actor": {"product": "codex", "session_id": "executor-session"}},
                   {"actor": {"product": "claude", "session_id": "other-initiator"}},
                   {"attempt_number": 2}, {"requirements_hash": "b" * 64},
                   {"snapshot_hash": "b" * 64}, {"reviewed_paths": ["src/a.py"]},
                   {"test_evidence": []},
                   {"test_evidence": [self.evidence(command="tests", exit_code=1)]}]
        original = self.read()
        for change in changes:
            with self.subTest(change=change), self.assertRaises(task_state.StateError):
                self.store.record_review("task", **dict(args, **change))
            self.assertEqual(self.read(), original)
        passed = self.store.record_review("task", **args)
        self.assertEqual(passed["status"], "accepted")
        self.assertEqual(passed["review"]["actor"], self.initiator)
        self.assertEqual(passed["review"]["snapshot_hash"], "a" * 64)

    def test_altered_completion_or_delivery_evidence_cannot_be_accepted(self):
        for which in ("completion_evidence", "delivery"):
            with self.subTest(which=which):
                # Each iteration has its own task store and completed delivery.
                if self.read()["attempts"]:
                    self.store.update_requirements("task", "Try again", actor=self.initiator)
                data = self.delivery()
                attempt = data["attempts"][-1]
                reference = attempt[which] if which == "completion_evidence" else attempt[which]["evidence"]
                Path(reference["path"]).write_bytes(b"Changed evidence")
                with self.assertRaisesRegex(task_state.StateError, "SHA256 changed"):
                    self.store.record_review("task", **self.review_args(data))
                self.assertNotEqual(self.read()["status"], "accepted")

    def test_new_attempt_revokes_pass_but_preserves_historical_review(self):
        passed = self.accept()
        old_review = passed["reviews"][0]
        updated = self.begin()
        self.assertNotIn("review", updated)
        self.assertEqual(updated["reviews"][0]["id"], old_review["id"])
        self.assertFalse(updated["reviews"][0]["active"])
        self.assertEqual(updated["reviews"][0]["invalidation_reason"], "new_attempt")
        self.assertEqual(updated["executor"]["session_id"], "executor-session")
        self.assertEqual(len(updated["attempts"]), 2)

    def test_feedback_versions_requirements_and_revokes_even_an_identical_text_pass(self):
        passed = self.accept()
        updated = self.store.update_requirements("task", passed["requirements"]["text"], actor=self.initiator)
        self.assertEqual(updated["requirements"]["version"], 2)
        self.assertEqual(len(updated["requirements_history"]), 2)
        self.assertEqual(updated["requirements"]["hash"], passed["requirements"]["hash"])
        self.assertEqual(updated["status"], "revision_requested")
        self.assertFalse(updated["reviews"][0]["active"])
        with self.assertRaisesRegex(task_state.StateError, "current requirements"):
            self.store.record_review("task", **self.review_args(passed))
        self.assertEqual(self.begin()["attempts"][-1]["requirements_version"], 2)

    def test_feedback_during_execution_does_not_clear_uncertainty_or_deadlock_rework(self):
        data = self.bind()
        updated = self.store.update_requirements("task", "New feedback on the same task", actor=self.initiator)
        self.assertTrue(updated["unresolved"])
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            self.begin()
        completed = self.terminal(data)
        delivered = self.delivery(completed)
        with self.assertRaisesRegex(task_state.StateError, "current requirements"):
            self.store.record_review("task", **self.review_args(delivered))
        self.assertEqual(self.begin()["attempts"][-1]["requirements_version"], 2)

    def test_rejected_delivery_can_rework_in_same_session_with_fresh_review(self):
        data = self.delivery()
        with self.assertRaisesRegex(task_state.StateError, "review the delivery"):
            self.begin()
        self.store.record_review("task", **dict(self.review_args(data), decision="revise", reviewed_paths=[], test_evidence=[]))
        revised = self.delivery()
        passed = self.accept(revised)
        self.assertEqual(passed["executor"]["session_id"], "executor-session")
        self.assertEqual(passed["review"]["attempt"], 2)
        self.assertEqual(len(passed["reviews"]), 2)
        self.assertFalse(passed["reviews"][0]["active"])
        self.assertTrue(passed["reviews"][1]["active"])

    def test_changed_delivery_revokes_pass_and_requires_all_final_paths(self):
        self.accept()
        replaced = self.store.record_delivery("task", 1, "b" * 64, ["src/a.py", "new.bin", "removed.txt"],
                                              self.evidence(b"Updated final snapshot"))
        self.assertEqual(replaced["status"], "needs_review")
        self.assertFalse(replaced["reviews"][0]["active"])
        self.assertEqual(len(replaced["attempts"][0]["delivery_history"]), 2)
        args = self.review_args(replaced)
        args["reviewed_paths"].remove("new.bin")
        with self.assertRaisesRegex(task_state.StateError, "every final changed path"):
            self.store.record_review("task", **args)

    def test_paths_and_task_ids_cannot_escape_state_or_project(self):
        for task_id in ("../escape", "/absolute", "", "a" * 81):
            with self.subTest(task_id=task_id), self.assertRaises(task_state.StateError):
                self.create(task_id)
        completed = self.terminal()
        for path in ("../outside", "/outside", "a/../outside", ".git/config", "a//b"):
            with self.subTest(path=path), self.assertRaises(task_state.StateError):
                self.delivery(completed, paths=[path])


if __name__ == "__main__":
    unittest.main()
