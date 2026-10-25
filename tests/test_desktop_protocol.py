"""Offline regressions for Codex desktop framing, isolation and bootstrap.

Socket pairs carry test frames only. Bootstrap runs against an in-process JSON
RPC double backed by anonymous pipes; no executable or desktop is contacted.
"""

import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "claudex-bridge" / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "claudex_desktop_protocol_under_test", SCRIPTS / "desktop_bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

THREAD = "00000000-0000-4000-8000-000000000001"
OTHER_THREAD = "00000000-0000-4000-8000-000000000002"
REQUEST = "00000000-0000-4000-8000-000000000003"
OWNER = "desktop-owner"


def frame(message):
    raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.patch(bridge.subprocess, "run", side_effect=AssertionError("Real launch forbidden"))
        self.patch(bridge.subprocess, "Popen", side_effect=AssertionError("Real process forbidden"))
        self.patch(socket.socket, "connect", side_effect=AssertionError("Real IPC forbidden"))

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result


class DesktopProtocolTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.client, self.peer = socket.socketpair()
        self.addCleanup(self.client.close)
        self.addCleanup(self.peer.close)
        self.peer.settimeout(1)
        self.log = io.StringIO()
        self.desktop = bridge.Desktop(self.log)
        self.desktop.sock = self.client
        self.desktop.thread_id = THREAD
        self.desktop.owner = OWNER
        self.desktop.client_id = "bridge-client"

    def snapshot_message(self, **overrides):
        message = {
            "type": "broadcast", "sourceClientId": OWNER,
            "method": "thread-stream-state-changed",
            "version": bridge.VERSIONS["thread-stream-state-changed"],
            "params": {"hostId": "local", "conversationId": THREAD,
                       "change": {"type": "snapshot", "revision": 3,
                                  "conversationState": {"id": THREAD, "turns": []}}},
        }
        message.update(overrides)
        return message

    def queue_reply(self, owner=OWNER, request_id=REQUEST, result=None):
        self.peer.sendall(frame({
            "type": "response", "requestId": request_id,
            "resultType": "success", "handledByClientId": owner,
            "result": result if result is not None else {"ok": True},
        }))

    def read_sent_request(self):
        def read_exact(size):
            result = bytearray()
            while len(result) < size:
                chunk = self.peer.recv(size - len(result))
                self.assertTrue(chunk, "The test socket closed before the full request")
                result.extend(chunk)
            return bytes(result)

        size = struct.unpack("<I", read_exact(4))[0]
        return json.loads(read_exact(size))

    def test_fragmented_header_and_utf8_body_survive_receive_timeouts(self):
        expected = {"type": "response", "result": "中文进度与工具结果"}
        encoded = frame(expected)
        split_in_utf8 = encoded.index("中".encode("utf-8")) + 1
        for chunk in (encoded[:2], encoded[2:split_in_utf8]):
            self.peer.sendall(chunk)
            self.assertIsNone(self.desktop.receive(0.01))
        self.peer.sendall(encoded[split_in_utf8:])
        self.assertEqual(self.desktop.receive(1), expected)
        self.assertEqual(self.desktop.buffer, bytearray())

    def test_coalesced_frames_are_delivered_separately_and_in_order(self):
        first = {"type": "notice", "sequence": 1}
        second = {"type": "notice", "sequence": 2}
        self.peer.sendall(frame(first) + frame(second))
        self.assertEqual(self.desktop.receive(1), first)
        self.assertEqual(self.desktop.receive(1), second)
        self.assertIsNone(self.desktop.receive(0.01))

    def test_invalid_frame_length_and_disconnect_are_not_completion(self):
        self.peer.sendall(struct.pack("<I", bridge.MAX_FRAME + 1))
        with self.assertRaisesRegex(bridge.BridgeError, "frame length"):
            self.desktop.receive(1)
        self.desktop.buffer.clear()
        self.peer.close()
        with self.assertRaisesRegex(bridge.BridgeError, "may still be running"):
            self.desktop.receive(1)
        self.assertIsNone(self.desktop.state)

    def test_foreign_owner_conversation_and_host_cannot_replace_state(self):
        original = {"id": THREAD, "turns": [{"turnId": "original"}]}
        self.desktop.state, self.desktop.revision = original, 2
        foreign_owner = self.snapshot_message(sourceClientId="another-owner", version=999)
        foreign_thread = self.snapshot_message(version=999)
        foreign_thread["params"]["conversationId"] = OTHER_THREAD
        foreign_host = self.snapshot_message(version=999)
        foreign_host["params"]["hostId"] = "another-host"
        for message in (foreign_owner, foreign_thread, foreign_host):
            with self.subTest(message=message):
                self.desktop.handle(message)
                self.assertIs(self.desktop.state, original)
                self.assertEqual(self.desktop.revision, 2)
        self.assertEqual(self.log.getvalue(), "")

    def test_owned_broadcast_with_changed_version_fails_closed(self):
        with self.assertRaisesRegex(bridge.BridgeError, "broadcast version"):
            self.desktop.handle(self.snapshot_message(version=999))
        self.assertIsNone(self.desktop.state)
        self.assertEqual(self.desktop.revision, -1)

    def test_snapshot_payload_must_match_its_conversation_envelope(self):
        message = self.snapshot_message()
        message["params"]["change"]["conversationState"]["id"] = OTHER_THREAD
        with self.assertRaisesRegex(bridge.BridgeError, "different conversation"):
            self.desktop.handle(message)
        self.assertIsNone(self.desktop.state)

    def test_request_skips_unrelated_response_but_processes_owner_progress(self):
        self.patch(bridge.uuid, "uuid4", return_value=REQUEST)
        self.queue_reply(request_id="not-this-request", result={"wrong": True})
        progress = self.snapshot_message()
        self.peer.sendall(frame(progress))
        self.queue_reply(result={"revision": 3})
        result = self.desktop.request("thread-follower-load-complete-history",
                                      {"conversationId": THREAD}, target=OWNER)
        self.assertEqual(result["result"], {"revision": 3})
        self.assertEqual(self.desktop.state, progress["params"]["change"]["conversationState"])
        self.assertEqual(self.desktop.revision, 3)
        self.assertEqual(self.read_sent_request()["targetClientId"], OWNER)

    def test_matching_response_from_another_owner_is_rejected(self):
        self.patch(bridge.uuid, "uuid4", return_value=REQUEST)
        self.queue_reply(owner="wrong-owner")
        with self.assertRaisesRegex(bridge.BridgeError, "different owner"):
            self.desktop.request("thread-follower-load-complete-history",
                                 {"conversationId": THREAD}, target=OWNER)

    def test_interrupt_targets_saved_turn_even_when_snapshot_contains_a_newer_turn(self):
        self.patch(bridge.uuid, "uuid4", return_value=REQUEST)
        self.desktop.state = {"id": THREAD, "turns": [{"turnId": "newer-user-turn"}]}
        self.queue_reply(result={"stopped": True})
        self.assertEqual(self.desktop.interrupt("original-worker-turn"), {"stopped": True})
        sent = self.read_sent_request()
        self.assertEqual(sent["targetClientId"], OWNER)
        self.assertEqual(sent["method"], "thread-follower-interrupt-turn")
        self.assertEqual(sent["params"]["conversationId"], THREAD)
        self.assertEqual(sent["params"]["expectedTurnId"], "original-worker-turn")

    def test_interrupt_without_exact_turn_sends_nothing(self):
        request = self.patch(self.desktop, "request")
        for turn_id in (None, ""):
            with self.subTest(turn_id=turn_id):
                with self.assertRaisesRegex(bridge.BridgeError, "exact original turn ID"):
                    self.desktop.interrupt(turn_id)
        request.assert_not_called()


class RpcInput:
    def __init__(self, process):
        self.process = process
        self.pending = bytearray()
        self.closed = False

    def write(self, data):
        self.pending.extend(data)
        while b"\n" in self.pending:
            line, _, rest = self.pending.partition(b"\n")
            self.pending[:] = rest
            self.process.on_message(json.loads(line))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class RpcProcess:
    """Anonymous pipes preserve the real bootstrap read/framing path."""

    def __init__(self, handle_request):
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin = RpcInput(self)
        self.handle_request = handle_request
        self.messages = []
        self.waited = False

    def on_message(self, message):
        self.messages.append(message)
        if "id" not in message:
            return
        result = self.handle_request(message)
        # Progress and an unrelated RPC response may arrive before our reply.
        outgoing = [
            {"method": "bootstrap/progress", "params": {}},
            {"id": "unrelated", "result": {"not_the_reply": True}},
            {"id": message["id"], **result},
        ]
        os.write(self.write_fd, b"".join(json.dumps(item).encode() + b"\n" for item in outgoing))

    def wait(self, timeout=None):
        self.waited = True
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None
        return 0

    def close(self):
        self.wait()
        self.stdout.close()
        self.stdin.close()


@unittest.skipUnless(sys.platform == "linux", "Bootstrap is part of the retained Linux adapter")
class BootstrapProtocolTests(OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="claudex-protocol-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / "codex-home"
        (self.home / "sessions").mkdir(parents=True)
        self.rollout = self.home / "sessions" / "rollout.jsonl"
        self.rollout.touch()
        self.persisted = self.directory / "identity.json"
        self.patch(bridge, "codex_home", return_value=self.home)
        self.start_result = {"thread": {"id": THREAD, "path": str(self.rollout)},
                             "model": "selected-model", "reasoningEffort": "high"}
        self.fail_method = None
        self.process = RpcProcess(self.reply)
        self.addCleanup(self.process.close)
        self.launch = self.patch(bridge.subprocess, "Popen", return_value=self.process)
        self.remember = mock.Mock(side_effect=self.save_identity)

    def save_identity(self, thread_id, rollout_path):
        self.persisted.write_text(json.dumps({"thread": thread_id, "rollout": rollout_path}),
                                  encoding="utf-8")

    def reply(self, message):
        method = message["method"]
        if method == "thread/start":
            return {"result": self.start_result}
        if method.startswith("thread/"):
            # The identity and validated path must already be on disk before
            # any follow-up RPC can fail or lose its acknowledgement.
            self.assertEqual(json.loads(self.persisted.read_text(encoding="utf-8")),
                             {"thread": THREAD, "rollout": str(self.rollout)})
        if method == self.fail_method:
            return {"error": {"code": -32000, "message": "injected failure"}}
        return {"result": {}}

    def run_bootstrap(self):
        bridge.create_thread(self.directory / "never-executed-codex", str(self.directory),
                             "Offline fixture", self.directory, self.remember,
                             model="selected-model", effort="high")

    def methods(self):
        return [message["method"] for message in self.process.messages]

    def assert_no_model_turn(self):
        self.assertFalse(any(method.startswith("turn/") for method in self.methods()))
        self.assertEqual(self.methods().count("thread/start"), 1)
        self.assertTrue(self.process.waited)
        self.assertTrue(self.process.stdin.closed)
        self.assertTrue(self.process.stdout.closed)

    def test_bootstrap_retains_settings_and_releases_an_idle_persisted_thread(self):
        self.run_bootstrap()
        started = next(item for item in self.process.messages if item["method"] == "thread/start")
        self.assertEqual(started["params"]["model"], "selected-model")
        self.assertEqual(started["params"]["config"]["model_reasoning_effort"], "high")
        self.assertFalse(started["params"]["ephemeral"])
        self.assertEqual(started["params"]["cwd"], str(self.directory))
        self.assertEqual(self.remember.call_args_list,
                         [mock.call(THREAD, None), mock.call(THREAD, str(self.rollout))])
        self.assertEqual(self.methods()[-1], "thread/unsubscribe")
        self.assert_no_model_turn()

    def test_followup_rpc_failure_keeps_the_original_thread_recoverable(self):
        self.fail_method = "thread/inject_items"
        with self.assertRaisesRegex(bridge.BridgeError, "injected failure"):
            self.run_bootstrap()
        self.assertEqual(json.loads(self.persisted.read_text(encoding="utf-8")),
                         {"thread": THREAD, "rollout": str(self.rollout)})
        self.assertNotIn("thread/name/set", self.methods())
        self.assert_no_model_turn()

    def test_model_or_effort_mismatch_keeps_identity_but_never_submits(self):
        for key, wrong_value in (("model", "other-model"), ("reasoningEffort", "low")):
            with self.subTest(key=key):
                self.start_result[key] = wrong_value
                with self.assertRaisesRegex(bridge.BridgeError, "did not confirm model/effort"):
                    self.run_bootstrap()
                self.assertEqual(json.loads(self.persisted.read_text(encoding="utf-8")),
                                 {"thread": THREAD, "rollout": str(self.rollout)})
                self.assertNotIn("thread/inject_items", self.methods())
                self.assert_no_model_turn()
                self.start_result[key] = "selected-model" if key == "model" else "high"
                # Each independent failed bootstrap gets a fresh pipe double.
                self.process = RpcProcess(self.reply)
                self.addCleanup(self.process.close)
                self.launch.return_value = self.process

    def test_invalid_rollout_path_still_persists_created_identity_before_validation(self):
        self.start_result["thread"]["path"] = str(self.directory / "outside-sessions.jsonl")
        with self.assertRaisesRegex(bridge.BridgeError, "rollout"):
            self.run_bootstrap()
        self.remember.assert_called_once_with(THREAD, None)
        self.assertEqual(json.loads(self.persisted.read_text(encoding="utf-8")),
                         {"thread": THREAD, "rollout": None})
        self.assertNotIn("thread/inject_items", self.methods())
        self.assert_no_model_turn()

    def test_missing_rollout_path_still_persists_created_identity(self):
        del self.start_result["thread"]["path"]
        with self.assertRaises((bridge.BridgeError, KeyError)):
            self.run_bootstrap()
        self.remember.assert_called_once_with(THREAD, None)
        self.assertEqual(json.loads(self.persisted.read_text(encoding="utf-8")),
                         {"thread": THREAD, "rollout": None})
        self.assertNotIn("thread/inject_items", self.methods())
        self.assert_no_model_turn()


if __name__ == "__main__":
    unittest.main()
