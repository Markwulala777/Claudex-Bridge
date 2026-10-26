"""Real temporary JSONL/state files, fake Desktop; no model, IPC or user session."""

import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "claudex-bridge" / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    spec = importlib.util.spec_from_file_location("claudex_codex_owner_tested", SCRIPTS / "codex_owner.py")
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    import desktop_bridge
finally:
    sys.path.remove(str(SCRIPTS))


class FakeDesktop:
    """Exercise the public adapter with the retained transport interface only."""

    def __init__(self, state):
        self.state = state
        self.owner = "exact-desktop-owner"
        self.revision = 1
        self.starts = []
        self.interrupts = []
        self.attaches = []
        self.exits = 0
        self.start_effect = None
        self.snapshot_error = None
        self.interrupt_error = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.exits += 1

    def attach(self, session_id):
        self.attaches.append(session_id)
        return copy.deepcopy(self.state)

    def snapshot(self):
        if self.snapshot_error:
            raise self.snapshot_error
        return copy.deepcopy(self.state)

    def start(self, prompt, message_id):
        self.starts.append((prompt, message_id))
        if self.start_effect:
            return self.start_effect(prompt, message_id)
        self.state["turns"].append({"turnId": "original-turn", "status": "inProgress",
                                    "params": {"clientUserMessageId": message_id}})
        return "original-turn"

    def interrupt(self, turn_id):
        self.interrupts.append(turn_id)
        if self.interrupt_error:
            raise self.interrupt_error
        return {"interrupted": True}


@unittest.skipUnless(sys.platform == "linux", "The retained Codex Desktop owner is Linux only")
class CodexOwnerTests(unittest.TestCase):
    SESSION = "00000000-0000-4000-8000-000000000001"
    OTHER_SESSION = "00000000-0000-4000-8000-000000000002"
    CLIENT = "original-client-message"
    TURN = "original-turn"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claudex-codex-owner-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "codex-home"
        (self.home / "sessions").mkdir(parents=True)
        self.rollout = self.home / "sessions" / "rollout.jsonl"
        self.append({"type": "session_meta", "payload": {"id": self.SESSION}})
        self.directory = self.base / "task" / "attempt-001"
        self.desktop = FakeDesktop(self.state())
        self.patch(owner, "Desktop", return_value=self.desktop)
        self.verify = self.patch(owner, "verify_installation", return_value=self.base / "unused-codex")
        self.patch(desktop_bridge, "codex_home", return_value=self.home)
        self.patch(desktop_bridge.socket, "socket", side_effect=AssertionError("Real IPC is forbidden"))
        self.patch(desktop_bridge.subprocess, "run", side_effect=AssertionError("Real processes are forbidden"))
        self.patch(desktop_bridge.subprocess, "Popen", side_effect=AssertionError("Real processes are forbidden"))

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def adapter(self):
        return owner.CodexOwner(self.SESSION, self.rollout, self.directory)

    def state(self, *turns, requests=None):
        return {"id": self.SESSION, "turns": list(turns), "requests": requests or []}

    def turn(self, turn_id=TURN, message_id=CLIENT, status="inProgress"):
        return {"turnId": turn_id, "status": status, "params": {"clientUserMessageId": message_id}}

    def append(self, value, *, newline=True):
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8") + (b"\n" if newline else b"")
        with self.rollout.open("ab") as stream:
            stream.write(raw)
        return raw

    def completion(self, turn_id=TURN, text="Implemented and tested the requested change", **kwargs):
        return {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": turn_id, "last_agent_message": text, **kwargs}}

    def record(self):
        return json.loads((self.directory / "codex-owner.json").read_text())

    def submit(self, adapter):
        self.assertTrue(adapter.preflight()["ready"])
        self.assertEqual(adapter.submit("Implement the requested change", self.CLIENT), self.TURN)

    def test_construction_entry_and_preflight_leave_files_unchanged(self):
        before = self.rollout.read_bytes()
        adapter = self.adapter()
        self.assertFalse(self.directory.exists())
        with adapter:
            self.assertEqual(adapter.preflight()["session_id"], self.SESSION)
            self.assertFalse(self.directory.exists())
        self.assertEqual(self.rollout.read_bytes(), before)
        self.assertEqual(self.desktop.attaches, [self.SESSION])
        self.assertEqual(self.desktop.starts, [])
        self.assertEqual(self.desktop.interrupts, [])

    def test_preflight_rejects_running_turn_or_requests_without_writes(self):
        for state in (self.state(self.turn()), self.state(requests=[{"id": "permission"}])):
            with self.subTest(state=state):
                self.desktop.state = state
                with self.adapter() as adapter:
                    with self.assertRaisesRegex(owner.OwnerError, "in-progress|pending"):
                        adapter.preflight()
                self.assertFalse(self.directory.exists())
                self.assertEqual(self.desktop.starts, [])

    def test_missing_or_different_desktop_project_never_dispatches(self):
        for cwd in (None, "relative-project", str(self.home)):
            with self.subTest(cwd=cwd):
                self.desktop.state["cwd"] = cwd
                with self.assertRaisesRegex(owner.OwnerError, "working directory"):
                    with owner.CodexOwner(self.SESSION, self.rollout, self.directory,
                                          expected_project=self.base):
                        self.fail("Wrong project entered")
                self.assertFalse(self.directory.exists())
                self.assertEqual(self.desktop.starts, [])

    def test_project_change_after_preflight_blocks_send_without_reservation(self):
        self.desktop.state["cwd"] = str(self.base)
        with owner.CodexOwner(self.SESSION, self.rollout, self.directory,
                              expected_project=self.base) as adapter:
            self.assertTrue(adapter.preflight()["ready"])
            self.desktop.state["cwd"] = str(self.home)
            with self.assertRaisesRegex(owner.OwnerError, "working directory"):
                adapter.submit("must not use a different project", self.CLIENT)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.desktop.starts, [])

    def test_version_or_wrong_rollout_refused_before_connect_and_directory_creation(self):
        self.verify.side_effect = owner.BridgeError("Unsupported installed IPC version")
        with self.assertRaisesRegex(owner.BridgeError, "Unsupported"):
            with self.adapter():
                self.fail("Unknown installation entered")
        self.assertEqual(self.desktop.attaches, [])
        self.verify.side_effect = None
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.OTHER_SESSION}}) + "\n")
        with self.assertRaisesRegex(owner.OwnerError, "exact Codex session"):
            with self.adapter():
                self.fail("Wrong rollout entered")
        self.assertEqual(self.desktop.attaches, [])
        self.assertFalse(self.directory.exists())

    def test_unknown_is_durable_before_one_send_and_recovered_after_lost_ack(self):
        def lose_ack(_prompt, message_id):
            record = self.record()
            self.assertEqual(record["submission_state"], "unknown")
            self.assertIsNone(record["execution_id"])
            self.assertEqual(record["client_message_id"], message_id)
            self.desktop.state = self.state(self.turn())
            raise owner.BridgeError("Submission acknowledgment was lost")

        self.desktop.start_effect = lose_ack
        with self.adapter() as adapter:
            with self.assertRaisesRegex(owner.BridgeError, "acknowledgment"):
                adapter.submit("Implement once", self.CLIENT)
            with self.assertRaisesRegex(owner.OwnerError, "already reserved"):
                adapter.submit("Implement once", self.CLIENT)
        with self.adapter() as adapter:
            with self.assertRaisesRegex(owner.OwnerError, "already reserved"):
                adapter.submit("Implement once", self.CLIENT)
            observed = adapter.observe(self.CLIENT)
        self.assertEqual(observed["execution_id"], self.TURN)
        self.assertEqual(observed["state"], "running")
        self.assertEqual(self.record()["execution_id"], self.TURN)
        self.assertEqual(len(self.desktop.starts), 1)

    def test_preflight_race_existing_message_and_invalid_ack_never_trigger_retry(self):
        with self.adapter() as adapter:
            adapter.preflight()
            self.desktop.state = self.state(self.turn("native-other", "other"))
            with self.assertRaisesRegex(owner.OwnerError, "not idle"):
                adapter.submit("Work", self.CLIENT)
            self.assertFalse(self.directory.exists())
            self.desktop.state = self.state(self.turn(status="completed"))
            with self.assertRaisesRegex(owner.OwnerError, "already exists"):
                adapter.submit("Work", self.CLIENT)
            self.desktop.state = self.state()
            self.desktop.start_effect = lambda *_args: ""
            with self.assertRaisesRegex(owner.OwnerError, "returned turn ID"):
                adapter.submit("Work", self.CLIENT)
            with self.assertRaisesRegex(owner.OwnerError, "already reserved"):
                adapter.submit("Work", self.CLIENT)
        self.assertEqual(len(self.desktop.starts), 1)
        self.assertEqual(self.record()["submission_state"], "unknown")

    def test_recovery_zero_or_multiple_client_matches_stays_unknown_and_never_sends(self):
        with self.adapter() as adapter:
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            self.desktop.state = self.state(self.turn("first"), self.turn("second"))
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            self.assertIsNone(self.record()["execution_id"])
            with self.assertRaisesRegex(owner.OwnerError, "already reserved"):
                adapter.submit("Must not retry", self.CLIENT)
        self.assertEqual(self.desktop.starts, [])

    def test_recovery_can_find_paginated_turn_but_supplied_id_cannot_select_another(self):
        self.desktop.state["turnHistory"] = {"history": {"entitiesByKey": {self.TURN: self.turn()}}}
        with self.adapter() as adapter:
            self.assertEqual(adapter.observe(self.CLIENT, "wrong-turn")["state"], "unknown")
            self.assertIsNone(self.record()["execution_id"])
            result = adapter.observe(self.CLIENT, self.TURN)
            self.assertEqual(result["execution_id"], self.TURN)
            self.assertEqual(result["state"], "running")
        self.assertEqual(self.desktop.starts, [])

    def test_bound_turn_is_never_replaced_by_newer_matching_message(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            # Native full history can omit/change old client metadata. The saved
            # exact turn from the acknowledgment must remain the binding.
            self.desktop.state = self.state(self.turn("new-turn", self.CLIENT))
            self.desktop.state["turnHistory"] = {"history": {"entitiesByKey": {
                self.TURN: self.turn(message_id="metadata-changed", status="completed")}}}
            self.append(self.completion())
            result = adapter.observe(self.CLIENT)
            self.assertEqual(result["execution_id"], self.TURN)
            self.assertEqual(result["state"], "terminal")
            with self.assertRaisesRegex(owner.OwnerError, "original bound turn"):
                adapter.observe(self.CLIENT, "new-turn")
            with self.assertRaisesRegex(owner.OwnerError, "original message"):
                adapter.observe("new-client", self.TURN)

    def test_completion_requires_fresh_desktop_and_exact_complete_rollout(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion("other-turn", "Unrelated completion"))
            self.desktop.state = self.state(self.turn(status="completed"))
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            self.append(self.completion(), newline=False)
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            self.assertFalse((self.directory / "codex-terminal.json").exists())
            with self.rollout.open("ab") as stream:
                stream.write(b"\n")
            self.desktop.snapshot_error = owner.BridgeError("IPC disconnected")
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            self.desktop.snapshot_error = None
            self.desktop.state = self.state(self.turn())
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "running")
            self.desktop.state = self.state(self.turn(status="completed"))
            result = adapter.observe(self.CLIENT)
            self.assertEqual(result["state"], "terminal")
            self.assertEqual(result["terminal_status"], "completed")
            self.assertEqual(result["result"], self.completion()["payload"]["last_agent_message"])
            evidence = result["evidence"]
            raw = Path(evidence["path"]).read_bytes()
            self.assertEqual(evidence["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(evidence["event_type"], "task_complete")
            saved = json.loads(raw)
            self.assertEqual(saved["desktop"]["turn"]["turnId"], self.TURN)
            self.assertEqual(saved["rollout"]["event"], self.completion())
            self.assertEqual(Path(evidence["path"]).stat().st_mode & 0o777, 0o600)
            self.desktop.revision += 10
            self.assertEqual(adapter.observe(self.CLIENT)["evidence"], evidence)

    def test_empty_error_duplicate_and_pre_submission_completions_are_not_success(self):
        cases = [self.completion(text=" "), self.completion(error="native failure"),
                 self.completion(isAborted=True), self.completion(status="interrupted")]
        for index, event in enumerate(cases):
            with self.subTest(event=event):
                self.directory = self.base / ("attempt-" + str(index))
                self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.SESSION}}) + "\n")
                self.desktop.state = self.state()
                with self.adapter() as adapter:
                    self.submit(adapter)
                    self.append(event)
                    self.desktop.state = self.state(self.turn(status="completed"))
                    self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
        self.directory = self.base / "duplicate"
        self.desktop.state = self.state()
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion())
            self.append(self.completion())
            self.desktop.state = self.state(self.turn(status="completed"))
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")

        self.directory = self.base / "stale"
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.SESSION}}) + "\n")
        self.append(self.completion())
        self.desktop.state = self.state()
        with self.adapter() as adapter:
            self.submit(adapter)
            self.desktop.state = self.state(self.turn(status="completed"))
            result = adapter.observe(self.CLIENT)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("predates", result["error"])

    def test_failed_and_interrupted_save_actual_desktop_terminal_without_success_result(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                self.directory = self.base / status
                self.desktop.state = self.state()
                with self.adapter() as adapter:
                    self.submit(adapter)
                    self.desktop.state = self.state(self.turn(status=status))
                    result = adapter.observe(self.CLIENT)
                self.assertEqual(result["state"], "terminal")
                self.assertEqual(result["terminal_status"], status)
                self.assertEqual(result["result"], "")
                self.assertEqual(result["evidence"]["event_type"], "codex_desktop_terminal")
                saved = json.loads(Path(result["evidence"]["path"]).read_text())
                self.assertNotIn("rollout", saved)
                self.assertEqual(saved["desktop"]["turn"]["status"], status)

    def test_waiting_only_applies_to_exact_turn_or_unspecified_request(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            for request, expected in (({"params": {"turnId": "other"}}, "running"),
                                      ({"params": {"turnId": self.TURN}}, "waiting"),
                                      ({"id": "unscoped-request"}, "waiting")):
                self.desktop.state["requests"] = [request]
                self.assertEqual(adapter.observe(self.CLIENT)["state"], expected)

    def test_truncation_replacement_and_rewrite_never_complete_including_reconnect(self):
        for attack in ("truncate", "replace", "rewrite"):
            with self.subTest(attack=attack):
                self.directory = self.base / attack
                self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.SESSION}}) + "\n")
                self.desktop.state = self.state()
                with self.adapter() as adapter:
                    self.submit(adapter)
                    self.append({"type": "event_msg", "payload": {"type": "agent_message", "message": "original"}})
                    self.assertEqual(adapter.observe(self.CLIENT)["state"], "running")
                    if attack == "truncate":
                        self.rollout.write_bytes(b"")
                    elif attack == "replace":
                        replacement = self.rollout.with_suffix(".replacement")
                        replacement.write_bytes(self.rollout.read_bytes())
                        os.replace(replacement, self.rollout)
                    else:
                        self.rollout.write_bytes(self.rollout.read_bytes().replace(b"original", b"modified"))
                    self.append(self.completion())
                    self.desktop.state = self.state(self.turn(status="completed"))
                    result = adapter.observe(self.CLIENT)
                    self.assertEqual(result["state"], "unknown")
                    self.assertIsNone(result["evidence"])
                count = len(self.desktop.attaches)
                with self.assertRaises(owner.OwnerError):
                    with self.adapter():
                        self.fail("Corrupted source reconnected")
                self.assertEqual(len(self.desktop.attaches), count)

    def test_observed_partial_bytes_cannot_be_rewritten_before_completing_line(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion(text="first"), newline=False)
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "running")
            self.rollout.write_bytes(self.rollout.read_bytes().replace(b"first", b"other") + b"\n")
            self.desktop.state = self.state(self.turn(status="completed"))
            result = adapter.observe(self.CLIENT)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("rewritten", result["error"])

    def test_reconnect_entry_pin_is_not_reset_to_older_persisted_prefix(self):
        with self.adapter() as adapter:
            self.submit(adapter)
        # These bytes were not in the persisted cursor, but the next read-only
        # context entry has already seen them. observe must preserve that pin.
        self.append({"type": "event_msg", "payload": {"type": "agent_message", "message": "original"}})
        with self.adapter() as adapter:
            self.rollout.write_bytes(self.rollout.read_bytes().replace(b"original", b"modified"))
            self.append(self.completion())
            self.desktop.state = self.state(self.turn(status="completed"))
            result = adapter.observe(self.CLIENT)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("rewritten", result["error"])

    def test_failed_reservation_cannot_send_and_wrong_attempt_binding_cannot_attach(self):
        with self.adapter() as adapter:
            with mock.patch.object(owner, "_write", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    adapter.submit("Work", self.CLIENT)
            self.assertEqual(self.desktop.starts, [])
            self.submit(adapter)
        record = self.record()
        record["session_id"] = self.OTHER_SESSION
        (self.directory / "codex-owner.json").write_text(json.dumps(record))
        count = len(self.desktop.attaches)
        with self.assertRaisesRegex(owner.OwnerError, "different session"):
            with self.adapter():
                self.fail("Different saved attempt entered")
        self.assertEqual(len(self.desktop.attaches), count)

    def test_malformed_or_oversized_rollout_stays_unknown(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            with self.rollout.open("ab") as stream:
                stream.write(b"not-json\n")
            self.desktop.state = self.state(self.turn(status="completed"))
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
        self.directory = self.base / "oversized"
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.SESSION}}) + "\n")
        self.desktop.state = self.state()
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion())
            self.desktop.state = self.state(self.turn(status="completed"))
            with mock.patch.object(owner, "MAX_ROLLOUT", 10):
                self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            with mock.patch.object(owner, "MAX_LINE", 10):
                self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")

    def test_snapshot_missing_wrong_unknown_status_never_uses_rollout_alone(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion())
            for state in (self.state(), {**self.state(), "id": self.OTHER_SESSION},
                          self.state(self.turn(status="newUnknownStatus"))):
                self.desktop.state = state
                self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")
            self.assertFalse((self.directory / "codex-terminal.json").exists())

    def test_interruption_requires_exact_original_live_turn_and_is_not_repeated(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            for client, turn in ((self.CLIENT, "other"), ("other", self.TURN)):
                with self.assertRaises(owner.OwnerError):
                    adapter.interrupt(client, turn)
            self.assertEqual(self.desktop.interrupts, [])
            reply = adapter.interrupt(self.CLIENT, self.TURN)
            self.assertTrue(reply["interrupt_requested"])
            self.assertEqual(self.desktop.interrupts, [self.TURN])
            with self.assertRaisesRegex(owner.OwnerError, "already requested"):
                adapter.interrupt(self.CLIENT, self.TURN)
        with self.adapter() as adapter:
            with self.assertRaisesRegex(owner.OwnerError, "already requested"):
                adapter.interrupt(self.CLIENT, self.TURN)
        self.assertEqual(self.desktop.interrupts, [self.TURN])

    def test_old_or_unobservable_turn_cannot_interrupt_new_turn(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.desktop.state = self.state(self.turn(status="completed"), self.turn("new", "new-message"))
            self.append(self.completion())
            with self.assertRaisesRegex(owner.OwnerError, "observably in progress"):
                adapter.interrupt(self.CLIENT, self.TURN)
            self.desktop.state = self.state(self.turn("new", "new-message"))
            with self.assertRaisesRegex(owner.OwnerError, "observably in progress"):
                adapter.interrupt(self.CLIENT, self.TURN)
            self.assertEqual(self.desktop.interrupts, [])

    def test_lost_interrupt_ack_does_not_repeat_and_never_means_terminal(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.desktop.interrupt_error = owner.BridgeError("Interrupt acknowledgment lost")
            with self.assertRaisesRegex(owner.BridgeError, "acknowledgment lost"):
                adapter.interrupt(self.CLIENT, self.TURN)
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "running")
            with self.assertRaisesRegex(owner.OwnerError, "already requested"):
                adapter.interrupt(self.CLIENT, self.TURN)
        self.assertEqual(self.desktop.interrupts, [self.TURN])

    def test_terminal_evidence_cannot_be_replaced_or_tampered(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion())
            self.desktop.state = self.state(self.turn(status="completed"))
            evidence = adapter.observe(self.CLIENT)["evidence"]
            path = Path(evidence["path"])
            value = json.loads(path.read_text())
            value["desktop"]["revision"] += 1
            path.write_text(json.dumps(value))
            observed = adapter.observe(self.CLIENT)
            self.assertEqual(observed["state"], "unknown")
            self.assertIn("evidence changed", observed["error"])
            self.desktop.state = self.state(self.turn(status="failed"))
            self.assertEqual(adapter.observe(self.CLIENT)["state"], "unknown")

    def test_terminal_with_live_attributed_command_stays_unresolved_until_it_exits(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            self.append(self.completion())
            self.desktop.state = self.state(self.turn(status="completed"))
            with mock.patch.object(owner.codex_processes, "candidates", return_value=[{"pid": 123}]):
                result = adapter.observe(self.CLIENT)
            self.assertEqual(result["state"], "unknown")
            self.assertIn("processes remain", result["error"])
            self.assertFalse((self.directory / "codex-terminal.json").exists())
            with mock.patch.object(owner.codex_processes, "candidates", return_value=[]):
                self.assertEqual(adapter.observe(self.CLIENT)["state"], "terminal")

    def test_turn_change_during_process_pinning_neither_interrupts_nor_signals(self):
        with self.adapter() as adapter:
            self.submit(adapter)
            pin = mock.MagicMock()
            def enter():
                self.desktop.state = self.state(self.turn(status="interrupted"), self.turn("new", "new-message"))
                return pin
            pin.__enter__.side_effect = enter
            with mock.patch.object(owner.codex_processes, "PinnedProcesses", return_value=pin):
                with self.assertRaisesRegex(owner.OwnerError, "changed before"):
                    adapter.interrupt(self.CLIENT, self.TURN)
            pin.terminate.assert_not_called()
            self.assertEqual(self.desktop.interrupts, [])


if __name__ == "__main__":
    unittest.main()
