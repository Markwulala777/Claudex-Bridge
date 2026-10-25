"""Offline workflow integration: real disposable Git/processes, native doubles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "claudex-bridge" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import task_state
import workflow


class AdapterDouble:
    def __init__(self, owner, task, transport, directory):
        self.owner, self.task, self.transport = owner, task, transport
        self.directory = Path(directory)

    def __enter__(self):
        self.owner.connects.append((self.task["executor"]["session_id"], self.directory))
        return self

    def __exit__(self, *_):
        pass

    def preflight(self):
        self.owner.preflights += 1
        if self.owner.preflight_error:
            raise RuntimeError(self.owner.preflight_error)
        return {"ready": True, "session_id": self.task["executor"]["session_id"]}

    def submit(self, prompt, client_message_id):
        state = self.owner.flow.store.read(self.task["task_id"])
        attempt = state["attempts"][-1]
        if not state["unresolved"] or attempt["submission_state"] != "unknown":
            raise AssertionError("Submit must follow durable unknown/unresolved state")
        if attempt["client_message_id"] != client_message_id:
            raise AssertionError("Submit must use the persisted client message ID")
        self.owner.sends.append((client_message_id, prompt))
        if self.owner.on_submit:
            self.owner.on_submit(self)
        if self.owner.submit_error:
            raise RuntimeError(self.owner.submit_error)
        return "turn-%s" % attempt["number"]

    def observe(self, client_message_id, execution_id=None):
        self.owner.observations.append((client_message_id, execution_id))
        task = self.owner.flow.store.read(self.task["task_id"])
        attempt = task["attempts"][-1]
        mode = self.owner.mode
        identity = dict(session_id=task["executor"]["session_id"],
                        execution_id="turn-%s" % attempt["number"], id_kind="turn")
        if mode == "unknown":
            return {"state": "unknown", "execution_id": None, "id_kind": "turn", "error": "No exact observed match"}
        if mode in ("running", "waiting"):
            return dict(identity, state=mode, evidence=None, terminal_status=None, result="")
        if mode == "eof":
            raise EOFError("Disconnected before exact completion")
        if mode == "wrong-session":
            return dict(identity, session_id="someone-else", state="running")
        status = {"success": "completed", "failure": "failed", "aborted": "aborted"}[mode]
        event_type = "task_complete" if status == "completed" else "codex_desktop_terminal"
        if task["executor"]["product"] == "claude":
            event_type = "turn.complete"
        metadata = dict(identity, status=status, event_type=event_type)
        path = self.directory / "native-terminal.json"
        if not path.exists():
            path.write_text(json.dumps(metadata))
        evidence = task_state.evidence_ref(path, **metadata)
        return dict(identity, state="terminal", terminal_status=status, evidence=evidence, result="Actual executor result")

    def interrupt(self, client_message_id, execution_id):
        self.owner.interrupts.append((client_message_id, execution_id))
        self.owner.mode = "aborted"


class DoubleFactory:
    def __init__(self):
        self.flow = None
        self.mode = "success"
        self.preflight_error = None
        self.submit_error = None
        self.on_submit = None
        self.preflights = 0
        self.connects, self.sends, self.observations, self.interrupts = [], [], [], []

    def __call__(self, task, transport, directory):
        return AdapterDouble(self, task, transport, directory)


@unittest.skipUnless(sys.platform == "linux", "Workflow execution currently uses the shared Linux lock")
class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claudex-workflow-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        self.patch_env = mock.patch.dict(os.environ, environment, clear=True)
        self.patch_env.start()
        self.addCleanup(self.patch_env.stop)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Offline Test")
        self.git("config", "user.email", "offline@example.invalid")
        self.git("config", "core.hooksPath", str(self.base / "absent-hooks"))
        (self.project / "app.txt").write_text("Original\n")
        self.git("add", "app.txt")
        self.git("commit", "--quiet", "-m", "Baseline")
        (self.project / "app.txt").write_text("Existing user edit\n")
        (self.project / "user-note.txt").write_text("Preserve this untracked user note\n")
        self.root = self.base / "state"
        self.factory = DoubleFactory()
        self.flow = workflow.Workflow(self.root, self.factory)
        self.factory.flow = self.flow
        self.initiator = {"product": "claude", "session_id": "initiating-claude"}
        self.executor = {"product": "codex", "session_id": "original-codex"}
        self.transport = {"kind": "codex-desktop", "rollout_path": str(self.base / "dummy-rollout")}
        self.factory.on_submit = self.implementation
        self.create()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.project), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout

    def create(self, **changes):
        values = dict(task_id="task", project=self.project, initiator=self.initiator,
                      executor=self.executor, requirements="Implement the requested change.", transport=self.transport)
        values.update(changes)
        return self.flow.create(**values)

    def implementation(self, adapter):
        (self.project / "app.txt").write_text("Existing user edit\nImplemented\n")
        (self.project / "new.bin").write_bytes(b"\x00\xffbinary")

    def dispatch(self, **options):
        return self.flow.dispatch("task", actor=self.initiator, wait_seconds=options.pop("wait_seconds", 0), **options)

    def run_test_command(self, code="print('actual passing test')", **options):
        return self.flow.record_test("task", [sys.executable, "-c", code], actor=self.initiator, **options)

    def review(self, **changes):
        task = self.flow.status("task")
        values = dict(actor=self.initiator, decision="pass",
                      reviewed_paths=task["attempts"][-1].get("delivery", {}).get("changed_paths", []),
                      notes="Read all final paths and actual test evidence.")
        values.update(changes)
        return self.flow.review("task", **values)

    def test_create_captures_existing_edits_and_untracked_files_in_immutable_baseline(self):
        data = self.flow.status("task")
        baseline = workflow._reference(data["workflow"]["baseline"])
        self.assertIn("app.txt", baseline["entries"])
        self.assertIn("user-note.txt", baseline["entries"])
        self.assertEqual(data["attempts"], [])
        original = (self.root / "task/workflow.json").read_bytes()
        self.assertEqual(self.create()["task_id"], "task")
        with self.assertRaisesRegex(workflow.WorkflowError, "transport cannot be replaced"):
            self.create(transport=dict(self.transport, rollout_path=str(self.base / "different-rollout")))
        self.assertEqual((self.root / "task/workflow.json").read_bytes(), original)

    def test_preflight_failure_creates_no_attempt_or_send(self):
        self.factory.preflight_error = "Native owner is busy"
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.dispatch()
        self.assertEqual(self.flow.status("task")["attempts"], [])
        self.assertEqual(self.factory.sends, [])

    def test_dispatch_once_builds_full_net_packet_then_actual_test_and_initiator_acceptance(self):
        task = self.dispatch()
        self.assertEqual(task["status"], "needs_review")
        self.assertEqual(len(self.factory.sends), 1)
        packet = workflow._reference(task["workflow"]["delivery_packet"])
        changes = {item["path"]: item for item in packet["comparison"]["changes"]}
        self.assertEqual(set(changes), {"app.txt", "new.bin"})
        self.assertTrue(changes["new.bin"]["binary"])
        self.assertIn(" Existing user edit", changes["app.txt"]["diff"])
        self.assertNotIn("-Original", changes["app.txt"]["diff"])
        test = self.run_test_command()
        self.assertEqual(test["exit_code"], 0)
        self.assertIn("actual passing test", Path(test["output"]["path"]).read_text())
        with self.assertRaisesRegex(workflow.WorkflowError, "initiating"):
            self.review(actor=self.executor)
        with self.assertRaisesRegex(task_state.StateError, "every final changed path"):
            self.review(reviewed_paths=["app.txt"])
        accepted = self.review()
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["review"]["actor"], self.initiator)
        self.assertEqual(accepted["review"]["test_evidence"][0]["snapshot_hash"], test["snapshot_hash"])
        self.assertEqual((self.project / "user-note.txt").read_text(), "Preserve this untracked user note\n")

    def test_lost_ack_and_unknown_submission_remain_unresolved_and_watch_never_resends(self):
        self.factory.submit_error = "Lost acknowledgment"
        self.factory.mode = "unknown"
        unknown = self.dispatch()
        client = unknown["attempts"][0]["client_message_id"]
        self.assertTrue(unknown["unresolved"])
        self.assertEqual(unknown["status"], "detached")
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            self.dispatch()
        self.assertEqual(len(self.factory.sends), 1)
        self.factory.mode = "success"
        recovered = workflow.Workflow(self.root, self.factory).watch("task", wait_seconds=0)
        self.assertEqual(recovered["status"], "needs_review")
        self.assertEqual(recovered["attempts"][0]["client_message_id"], client)
        self.assertEqual(len(recovered["attempts"]), 1)
        self.assertEqual(len(self.factory.sends), 1)
        connects = len(self.factory.connects)
        self.flow.watch("task", wait_seconds=0)
        self.assertEqual(len(self.factory.connects), connects)

    def test_eof_failure_wrong_session_and_permission_wait_do_not_pass(self):
        for mode in ("eof", "wrong-session", "waiting"):
            with self.subTest(mode=mode):
                self.factory.mode = mode
                if not self.factory.sends:
                    task = self.dispatch()
                else:
                    task = self.flow.watch("task", wait_seconds=0)
                self.assertTrue(task["unresolved"])
                self.assertNotIn("delivery", task["attempts"][0])
                with self.assertRaises(workflow.WorkflowError):
                    self.review()
        self.assertEqual(len(self.factory.sends), 1)
        self.factory.mode = "failure"
        failed = self.flow.watch("task", wait_seconds=0)
        self.assertFalse(failed["unresolved"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("delivery", failed["attempts"][0])

    def test_reverse_direction_and_rework_keep_same_executor_session(self):
        owner = {"product": "codex", "session_id": "initiating-codex"}
        executor = {"product": "claude", "session_id": "original-claude"}
        self.create(task_id="reverse", initiator=owner, executor=executor,
                    transport={"kind": "claude-native", "owner_state_dir": str(self.base / "claude-owner")})
        first = self.flow.dispatch("reverse", actor=owner, wait_seconds=0)
        self.flow.review("reverse", actor=owner, decision="revise", reviewed_paths=[], notes="Fix the remaining issue.")
        self.flow.feedback("reverse", "Implement the requested change and address the feedback.", actor=owner)
        second = self.flow.dispatch("reverse", actor=owner, wait_seconds=0)
        self.assertEqual(first["executor"], second["executor"])
        self.assertEqual(second["attempts"][-1]["number"], 2)
        self.assertFalse(second["reviews"][0]["active"])
        self.assertEqual(second["requirements"]["version"], 2)
        self.assertEqual([entry[0] for entry in self.factory.connects], ["original-claude", "original-claude"])

    def test_rejected_findings_reach_the_next_dispatch_without_changing_requirements(self):
        first = self.dispatch()
        findings = "Keep the user's text and reject empty input; add the missing empty-input test."
        self.review(decision="revise", reviewed_paths=["app.txt", "new.bin"], notes=findings)
        second = self.dispatch()
        self.assertEqual(second["executor"], first["executor"])
        self.assertEqual(second["requirements"], first["requirements"])
        prompt = self.factory.sends[-1][1]
        self.assertIn(findings, prompt)
        self.assertIn("rejected attempt 1", prompt)
        self.assertIn('"app.txt"', prompt)
        self.assertEqual(second["reviews"][0]["notes"], findings)

    def test_status_reports_stale_acceptance_without_overwriting_its_history(self):
        self.dispatch()
        self.run_test_command()
        accepted = self.review()
        self.assertTrue(accepted["acceptance_current"])
        before = (self.root / "task/task.json").read_bytes()
        (self.project / "app.txt").write_text("Later unreviewed work\n")
        stale = self.flow.status("task")
        self.assertEqual(stale["status"], "review_stale")
        self.assertEqual(stale["recorded_status"], "accepted")
        self.assertTrue(stale["review_stale"])
        self.assertFalse(stale["acceptance_current"])
        self.assertEqual((self.root / "task/task.json").read_bytes(), before)

    def test_review_fails_if_worktree_or_recorded_test_bytes_changed(self):
        self.dispatch()
        test = self.run_test_command()
        (self.project / "app.txt").write_text("Unreviewed later change\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "worktree changed"):
            self.review()
        refreshed = self.flow.refresh_delivery("task", actor=self.initiator)
        with self.assertRaisesRegex(workflow.WorkflowError, "another attempt, snapshot"):
            self.review(test_ids=[test["id"]])
        self.assertEqual(refreshed["status"], "needs_review")
        fresh = self.run_test_command()
        Path(fresh["output"]["path"]).write_text("Forged successful output")
        with self.assertRaisesRegex(workflow.WorkflowError, "output changed"):
            self.flow.review("task", actor=self.initiator, decision="pass", reviewed_paths=["app.txt", "new.bin"],
                              notes="Attempt to accept changed evidence")

    def test_failed_timed_out_and_worktree_mutating_tests_cannot_pass(self):
        self.dispatch()
        cases = [("import sys; print('failed'); sys.exit(7)", 10),
                 ("import time; time.sleep(30)", 0.05),
                 ("from pathlib import Path; Path('new.bin').write_bytes(b'changed')", 10)]
        for index, (code, timeout) in enumerate(cases):
            with self.subTest(code=code):
                if index:
                    self.flow.refresh_delivery("task", actor=self.initiator)
                result = self.run_test_command(code, timeout=timeout)
                if index == 0:
                    self.assertEqual(result["exit_code"], 7)
                elif index == 1:
                    self.assertTrue(result["timed_out"])
                    self.assertNotEqual(result["exit_code"], 0)
                else:
                    self.assertEqual(result["exit_code"], 0)
                    self.assertFalse(result["snapshot_verified_after"])
                with self.assertRaisesRegex(workflow.WorkflowError, "actual successful tests"):
                    self.review(test_ids=[result["id"]])

    def test_tests_run_argv_without_shell_and_do_not_accept_unknown_evidence_ids(self):
        self.dispatch()
        trap = self.base / "shell-was-used"
        text = "$(touch " + str(trap) + ") ; literal"
        result = self.flow.record_test("task", [sys.executable, "-c", "import sys; print(sys.argv[1])", text],
                                       actor=self.initiator)
        self.assertFalse(trap.exists())
        self.assertIn(text, Path(result["output"]["path"]).read_text())
        with self.assertRaisesRegex(workflow.WorkflowError, "unknown test"):
            self.review(test_ids=["not-a-recorded-test"])
        self.assertEqual(self.review(test_ids=[result["id"]])["status"], "accepted")

    def test_feedback_invalidates_old_acceptance_and_test_evidence(self):
        self.dispatch()
        old_test = self.run_test_command()
        accepted = self.review()
        self.assertEqual(accepted["status"], "accepted")
        changed = self.flow.feedback("task", "The next version of these requirements", actor=self.initiator)
        self.assertEqual(changed["status"], "revision_requested")
        self.assertFalse(changed["reviews"][0]["active"])
        with self.assertRaisesRegex(workflow.WorkflowError, "Requirements changed"):
            self.review(test_ids=[old_test["id"]])
        revised = self.dispatch()
        self.assertEqual(revised["executor"]["session_id"], "original-codex")
        with self.assertRaisesRegex(workflow.WorkflowError, "unknown test"):
            self.review(test_ids=[old_test["id"]])

    def test_stop_is_exact_unlocked_and_only_terminal_observation_resolves_it(self):
        self.factory.mode = "running"
        active = self.dispatch()
        attempt = active["attempts"][-1]
        # A separate holder represents the live monitor. stop must not contend.
        with task_state.worker_lock(self.root):
            stopped = self.flow.stop("task")
        self.assertTrue(stopped["workflow"]["stop_pending"])
        self.assertTrue(stopped["unresolved"])
        self.assertEqual(self.factory.interrupts, [])
        first_watch = self.flow.watch("task", wait_seconds=0)
        self.assertTrue(first_watch["unresolved"])
        self.assertEqual(self.factory.interrupts, [(attempt["client_message_id"], "turn-1")])
        terminal = self.flow.watch("task", wait_seconds=0)
        self.assertEqual(terminal["status"], "interrupted")
        self.assertFalse(terminal["unresolved"])
        self.assertEqual(len(self.factory.interrupts), 1)
        self.assertEqual(len(self.factory.sends), 1)

    def test_stop_refuses_unknown_execution_and_tampered_request_cannot_interrupt(self):
        self.factory.mode = "unknown"
        self.dispatch()
        with self.assertRaisesRegex(workflow.WorkflowError, "actual bound ID"):
            self.flow.stop("task")
        self.factory.mode = "running"
        self.flow.watch("task", wait_seconds=0)
        self.flow.stop("task")
        path = self.root / "task/attempt-001/stop-requested.json"
        request = json.loads(path.read_text())
        request["execution_id"] = "unrelated-running-turn"
        path.write_text(json.dumps(request))
        result = self.flow.watch("task", wait_seconds=0)
        self.assertTrue(result["unresolved"])
        self.assertIn("exact current execution", result["attempts"][0]["error"])
        self.assertEqual(self.factory.interrupts, [])


if __name__ == "__main__":
    unittest.main()
