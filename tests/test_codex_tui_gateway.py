"""Protocol-only tests. Fixture text is not a model result or live GUI evidence."""
import base64
import copy
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import codex_tui_gateway as gateway
finally:
    sys.path.remove(str(SCRIPTS))

THREAD = "00000000-0000-4000-8000-000000000001"
TURN = "00000000-0000-4000-8000-000000000002"
NEXT_TURN = "00000000-0000-4000-8000-000000000003"


class FixtureSource:
    def __init__(self, directory):
        self.thread_id = THREAD
        self.rollout = Path(directory) / "fixture-rollout.jsonl"
        self.codex_home = str(directory)
        self.meta = {"id": THREAD, "cwd": str(directory), "model_provider": "openai",
                     "cli_version": gateway.TUI_VERSION, "source": "vscode",
                     "timestamp": "2026-10-25T01:00:00Z"}
        self.state = {
            "id": THREAD, "cwd": str(directory), "latestModel": "offline-protocol-fixture",
            "modelProvider": "openai", "latestReasoningEffort": "high",
            "currentPermissions": {"approvalPolicy": "on-request", "approvalsReviewer": "user",
                                   "sandboxPolicy": {"type": "readOnly"}},
            "turns": [],
        }
        self.reads = 0
        self.failure = None

    def read(self):
        self.reads += 1
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.state)


def turn(text="first", status="inProgress", turn_id=TURN):
    return {"turnId": turn_id, "status": status,
            "params": {"input": [{"type": "text", "text": "offline user fixture", "text_elements": []}],
                       "clientUserMessageId": "fixture-client-id"},
            "items": [{"type": "agentMessage", "id": "agent-" + turn_id, "text": text}],
            "completedItemIds": {"agent-" + turn_id: status != "inProgress"}}


def initialize(rpc):
    return rpc.handle({"id": "init", "method": "initialize", "params": {
        "clientInfo": {"name": "codex-tui", "version": gateway.TUI_VERSION}}})


def client_frame(payload, opcode=1, final=True):
    mask = b"abcd"
    prefix = bytes([(0x80 if final else 0) | opcode])
    if len(payload) < 126:
        prefix += bytes([0x80 | len(payload)])
    elif len(payload) <= 65535:
        prefix += bytes([0x80 | 126]) + struct.pack("!H", len(payload))
    else:
        prefix += bytes([0x80 | 127]) + struct.pack("!Q", len(payload))
    return prefix + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claudex-gateway-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = FixtureSource(self.directory)

    def rpc(self):
        result = gateway.RPC(gateway.Mirror(self.source))
        self.assertIn("result", initialize(result))
        return result

    def test_resume_replays_original_history_without_starting_or_changing_execution(self):
        self.source.state["turns"] = [turn("original exact text", "completed")]
        rpc = self.rpc()
        before = copy.deepcopy(self.source.state)
        reads = self.source.reads
        response = rpc.handle({"id": 1, "method": "thread/resume", "params": {
            "threadId": THREAD, "excludeTurns": False, "model": None, "config": None}})
        thread = response["result"]["thread"]
        self.assertEqual(thread["id"], THREAD)
        self.assertEqual(thread["turns"][0]["items"][-1]["text"], "original exact text")
        self.assertFalse(thread["canAcceptDirectInput"])
        self.assertEqual(self.source.state, before)
        self.assertEqual(self.source.reads, reads)
        self.assertIsNotNone(rpc.subscription)

    def test_mutations_different_threads_and_resume_overrides_are_rejected(self):
        rpc = self.rpc()
        for method in ("turn/start", "turn/steer", "turn/interrupt", "thread/start", "thread/delete",
                       "thread/settings/update", "config/value/write", "account/login/start"):
            with self.subTest(method=method):
                response = rpc.handle({"id": 1, "method": method, "params": {"threadId": THREAD}})
                self.assertEqual(response["error"]["code"], -32601)
        for params in ({"threadId": "another-thread"}, {"threadId": THREAD, "model": "another-model"},
                       {"threadId": THREAD, "history": []}, {"threadId": THREAD, "path": "/another/path"}):
            with self.subTest(params=params):
                response = rpc.handle({"id": 2, "method": "thread/resume", "params": params})
                self.assertIn("error", response)
        self.assertEqual(self.source.reads, 1)

    def test_native_local_service_tier_default_is_allowed_only_when_it_matches_owner(self):
        self.source.state["latestThreadSettings"] = {"serviceTier": "priority"}
        self.source.state["turns"] = [turn("owner history", "completed")]
        rpc = self.rpc()
        original = copy.deepcopy(self.source.state)
        response = rpc.handle({"id": 1, "method": "thread/resume", "params": {
            "threadId": THREAD, "serviceTier": "priority", "excludeTurns": True}})
        self.assertEqual(response["result"]["serviceTier"], "priority")
        self.assertFalse(response["result"]["thread"]["canAcceptDirectInput"])
        self.assertIsNotNone(rpc.subscription)
        for tier in ("flex", "", {}, ["priority"]):
            with self.subTest(tier=tier):
                rejected = rpc.handle({"id": 2, "method": "thread/resume", "params": {
                    "threadId": THREAD, "serviceTier": tier}})
                self.assertIn("serviceTier", rejected["error"]["message"])
        self.assertEqual(self.source.reads, 1)
        self.assertEqual(self.source.state, original)

    def test_unknown_native_client_version_is_not_silently_accepted(self):
        rpc = gateway.RPC(gateway.Mirror(self.source))
        response = rpc.handle({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex-tui", "version": "0.0.0"}}})
        self.assertIn("error", response)
        self.assertFalse(rpc.initialized)

    def test_only_new_owner_text_is_streamed_and_completion_comes_from_owner(self):
        self.source.state["turns"] = [turn()]
        mirror = gateway.Mirror(self.source)
        subscription = gateway.Subscription(mirror)
        self.source.state["turns"][0]["items"][0]["text"] = "first second"
        mirror.refresh()
        messages = subscription.changes(mirror)
        deltas = [message["params"]["delta"] for message in messages
                  if message["method"] == "item/agentMessage/delta"]
        self.assertEqual(deltas, [" second"])
        self.assertNotIn("turn/completed", [message["method"] for message in messages])
        self.assertEqual(subscription.changes(mirror), [])

        self.source.state["turns"][0]["status"] = "completed"
        self.source.state["turns"][0]["completedItemIds"]["agent-" + TURN] = True
        mirror.refresh()
        messages = subscription.changes(mirror)
        self.assertEqual([message["method"] for message in messages],
                         ["thread/status/changed", "item/completed", "turn/completed"])
        self.assertEqual(messages[-1]["params"]["turn"]["items"][-1]["text"], "first second")
        self.assertEqual(subscription.changes(mirror), [])

    def test_command_output_and_new_turn_lifecycle_use_native_events(self):
        mirror = gateway.Mirror(self.source)
        subscription = gateway.Subscription(mirror)
        command = {"id": "command", "type": "commandExecution", "command": "offline fixture",
                   "commandActions": [], "cwd": str(self.directory), "status": "inProgress",
                   "aggregatedOutput": "first line\n"}
        current = turn()
        current["items"] = [command]
        current["completedItemIds"] = {"command": False}
        self.source.state["turns"] = [current]
        mirror.refresh()
        messages = subscription.changes(mirror)
        self.assertIn("turn/started", [message["method"] for message in messages])
        output = [message["params"]["delta"] for message in messages
                  if message["method"] == "item/commandExecution/outputDelta"]
        self.assertEqual(output, ["first line\n"])
        command["aggregatedOutput"] += "second line\n"
        mirror.refresh()
        messages = subscription.changes(mirror)
        self.assertEqual([message["params"]["delta"] for message in messages], ["second line\n"])

    def test_owner_failure_cannot_become_completion_and_rewrites_fail_closed(self):
        self.source.state["turns"] = [turn("original")]
        mirror = gateway.Mirror(self.source)
        subscription = gateway.Subscription(mirror)
        self.source.failure = gateway.BridgeError("owner disconnected")
        with self.assertRaisesRegex(gateway.BridgeError, "owner disconnected"):
            mirror.refresh()
        self.assertEqual(subscription.changes(mirror), [])
        self.source.failure = None
        self.source.state["turns"][0]["items"][0]["text"] = "rewritten"
        mirror.refresh()
        with self.assertRaisesRegex(gateway.ProtocolError, "rewrote"):
            subscription.changes(mirror)

    def test_unsubscribe_only_removes_view_subscription(self):
        rpc = self.rpc()
        rpc.dispatch("thread/resume", {"threadId": THREAD})
        before = copy.deepcopy(self.source.state)
        rpc.dispatch("thread/unsubscribe", {"threadId": THREAD})
        self.assertIsNone(rpc.subscription)
        self.assertEqual(self.source.state, before)
        self.assertEqual(self.source.reads, 1)

    def test_desktop_steering_and_answer_forms_preserve_only_acknowledged_human_text(self):
        current = turn("agent text", "completed")
        accepted = {"type": "steeringUserMessage", "id": "local-steering-id", "status": "accepted",
                    "serverUserMessageId": "server-steering-id", "serverClientUserMessageId": "client-steer",
                    "input": [{"type": "text", "text": "exact human steering", "text_elements": []}]}
        current["items"] = [accepted, {"type": "steered", "id": "server-steering-id"},
                            {"type": "userInputResponse", "id": "answers", "completed": True,
                             "answers": {"choice": ["exact human answer"]}},
                            {"type": "steeringUserMessage", "id": "pending", "status": "pending",
                             "input": [{"type": "text", "text": "not yet admitted"}]}]
        self.source.state["turns"] = [current]
        mirror = gateway.Mirror(self.source)
        items = mirror.thread["turns"][0]["items"]
        self.assertEqual([item["id"] for item in items], ["user-" + TURN, "server-steering-id", "answers"])
        self.assertEqual(items[1]["content"], accepted["input"])
        self.assertEqual(items[2]["content"][0]["text"], "exact human answer")
        self.assertEqual(current["items"][0]["type"], "steeringUserMessage")
        self.assertTrue(all(item["type"] == "userMessage" for item in items))
        with self.assertRaises(gateway.ProtocolError):
            gateway.normalise_item({**accepted, "serverUserMessageId": None})
        with self.assertRaises(gateway.ProtocolError):
            gateway.normalise_item({"type": "unrecognizedDecoration", "id": "unknown"})

    def test_new_original_retry_errors_keep_native_error_semantics_and_cannot_complete_turn(self):
        current = turn("partial")
        historic = {"type": "error", "id": "earlier-error", "message": "earlier retry", "willRetry": True}
        current["items"].append(historic)
        self.source.state["turns"] = [current]
        mirror = gateway.Mirror(self.source)
        subscription = gateway.Subscription(mirror)
        self.assertEqual(subscription.changes(mirror), [])
        original = {"type": "error", "id": "new-error", "message": "source disconnected",
                    "willRetry": True, "errorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
                    "additionalDetails": "original detail"}
        current["items"].append(original)
        mirror.refresh()
        events = subscription.changes(mirror)
        self.assertEqual([event["method"] for event in events], ["error"])
        self.assertEqual(events[0]["params"]["error"]["message"], original["message"])
        self.assertEqual(events[0]["params"]["error"]["codexErrorInfo"], original["errorInfo"])
        self.assertTrue(events[0]["params"]["willRetry"])
        self.assertEqual(subscription.changes(mirror), [])

    def test_rollout_header_must_match_exact_selected_conversation(self):
        self.source.rollout.write_text(json.dumps({"type": "session_meta", "payload": self.source.meta}) + "\n")
        self.assertEqual(gateway.read_session_meta(self.source.rollout, THREAD), self.source.meta)
        with self.assertRaises(gateway.BridgeError):
            gateway.read_session_meta(self.source.rollout, NEXT_TURN)

    @unittest.skipUnless(sys.platform == "linux", "Desktop source is Linux-specific")
    def test_source_discovers_existing_owner_without_open_resume_start_or_interrupt(self):
        self.source.rollout.write_text(json.dumps({"type": "session_meta", "payload": self.source.meta}) + "\n")
        desktop = mock.MagicMock(spec=gateway.Desktop)
        desktop.log = io_sink = __import__("io").StringIO()
        desktop.request.return_value = {"handledByClientId": "original-owner"}
        desktop.snapshot.return_value = self.source.state
        with mock.patch.object(gateway, "verify_installation"), \
                mock.patch.object(gateway, "validate_rollout", side_effect=Path), \
                mock.patch.object(gateway, "Desktop", return_value=desktop):
            with gateway.ExistingDesktopSource(THREAD, self.source.rollout) as source:
                self.assertEqual(source.read()["id"], THREAD)
        desktop.request.assert_called_once_with("thread-owner-discovery",
                                                {"hostId": "local", "conversationId": THREAD}, timeout=5)
        desktop.attach.assert_not_called()
        desktop.start.assert_not_called()
        desktop.interrupt.assert_not_called()
        desktop.__exit__.assert_called_once()
        self.assertEqual(io_sink.getvalue(), "")

    def test_fragmented_masked_messages_preserve_utf8_and_control_frames(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        websocket = gateway.WebSocket(a)
        message = json.dumps({"method": "initialized", "text": "中文"}, ensure_ascii=False).encode()
        split = len(message) - 4
        b.sendall(client_frame(message[:split], final=False) + client_frame(b"ping", opcode=9)
                  + client_frame(message[split:], opcode=0))
        self.assertEqual(websocket.receive(0.1), json.loads(message))
        self.assertEqual(b.recv(6), b"\x8a\x04ping")

    def test_unmasked_and_oversized_frames_are_rejected(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        websocket = gateway.WebSocket(a)
        websocket.buffer.extend(b"\x81\x02{}")
        with self.assertRaises(gateway.ProtocolError):
            websocket.take_frame()
        websocket.buffer.clear()
        websocket.buffer.extend(b"\x81\xff" + struct.pack("!Q", gateway.MAX_FRAME + 1))
        with self.assertRaises(gateway.ProtocolError):
            websocket.take_frame()


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("CLAUDEX_TEST_NATIVE_TUI"),
                     "Opt in with CLAUDEX_TEST_NATIVE_TUI=/path/to/checked/codex; no model is run")
class NativeTUIProtocolTests(unittest.TestCase):
    def test_native_tui_replays_fixture_history_and_renders_later_protocol_deltas(self):
        """A real TUI consumes explicit offline fixture content, not a real session."""
        import fcntl
        import pty
        import select
        import termios

        with tempfile.TemporaryDirectory(prefix="claudex-native-protocol-") as temporary:
            directory = Path(temporary)
            source = FixtureSource(directory)
            # A real TUI sends a stored service tier on read-only remote resume.
            # Keep the fixture isolated, but exercise this non-empty local default.
            (directory / "config.toml").write_text('service_tier = "priority"\n')
            source.state["latestThreadSettings"] = {"serviceTier": "priority"}
            source.state["turns"] = [turn("OFFLINE_HISTORY_MARKER", "completed")]
            mirror = gateway.Mirror(source)
            path = str(directory / "gateway.sock")
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen(1)
            listener.settimeout(10)
            requests, sent, errors = [], [], []
            actual_send = gateway.WebSocket.send

            def observe_send(websocket, message):
                sent.append(copy.deepcopy(message))
                return actual_send(websocket, message)

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        gateway.serve_connection(connection, mirror, poll_interval=0.05, on_request=requests.append)
                except EOFError:
                    pass
                except BaseException as exc:
                    errors.append(exc)

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            output = bytearray()
            process = None
            server = None
            stage = 0
            with mock.patch.object(gateway.WebSocket, "send", new=observe_send):
                try:
                    server = threading.Thread(target=serve, daemon=True)
                    server.start()
                    # The child has isolated config/history/logs and receives no
                    # user prompt, key, auth file or provider configuration.
                    environment = dict(os.environ, TERM="xterm-256color", CODEX_HOME=temporary)
                    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
                        environment.pop(name, None)
                    process = subprocess.Popen([
                        os.environ["CLAUDEX_TEST_NATIVE_TUI"], "--remote", "unix://" + path,
                        "resume", THREAD, "--no-alt-screen",
                    ], cwd=temporary, stdin=slave, stdout=slave, stderr=slave,
                        env=environment, start_new_session=True)
                    os.close(slave)
                    slave = None
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and process.poll() is None:
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError:
                                break
                            output.extend(chunk)
                            if b"\x1b[6n" in chunk:
                                os.write(master, b"\x1b[1;1R")
                        if stage == 0 and b"OFFLINE_HISTORY_MARKER" in output:
                            source.state["turns"].append(turn("OFFLINE_LIVE_ONE", turn_id=NEXT_TURN))
                            stage = 1
                        elif stage == 1 and b"OFFLINE_LIVE_ONE" in output:
                            source.state["turns"][-1]["items"][0]["text"] += " OFFLINE_LIVE_TWO"
                            stage = 2
                        elif stage == 2 and b"OFFLINE_LIVE_TWO" in output:
                            source.state["turns"][-1]["status"] = "completed"
                            source.state["turns"][-1]["completedItemIds"]["agent-" + NEXT_TURN] = True
                            stage = 3
                        elif stage == 3 and any(message.get("method") == "turn/completed" for message in sent):
                            break
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    if slave is not None:
                        os.close(slave)
                    os.close(master)
                    listener.close()
                    if server is not None:
                        server.join(timeout=2)
            self.assertFalse(errors, str(errors))
            self.assertEqual(stage, 3, output.decode(errors="replace")[-10000:])
            self.assertIn(b"OFFLINE_HISTORY_MARKER", output)
            self.assertIn(b"OFFLINE_LIVE_ONE", output)
            self.assertIn(b"OFFLINE_LIVE_TWO", output)
            methods = [request.get("method") for request in requests]
            self.assertEqual(methods.count("thread/resume"), 1)
            self.assertEqual(next(request["params"]["serviceTier"] for request in requests
                                  if request.get("method") == "thread/resume"), "priority")
            self.assertNotIn("turn/start", methods)
            self.assertNotIn("thread/start", methods, json.dumps(requests, indent=2))
            self.assertTrue(any(message.get("method") == "turn/completed" for message in sent))


if __name__ == "__main__":
    unittest.main()
