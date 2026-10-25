"""Offline checks of the pinned Claude SDK follower; no Claude is executed."""

import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts/claude_vscode_proxy.py"
SPEC = importlib.util.spec_from_file_location("claude_vscode_proxy", SCRIPT)
proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proxy
SPEC.loader.exec_module(proxy)

SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def uid():
    return str(uuid.uuid4())


def wire(row):
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def transcript(kind="user", content="需求", parent=None, message_id="msg_fixture"):
    body = {"role": kind, "content": content}
    if kind == "assistant":
        body.update({"id": message_id, "type": "message", "model": "fixture-model"})
    return {"type": kind, "sessionId": SESSION, "version": "2.1.295", "uuid": uid(),
            "timestamp": "2026-10-25T00:00:00Z", "parentUuid": parent,
            "isSidechain": False, "message": body}


def sdk_init():
    # Real field names from the bundled CLI O0 schema/tJe producer. This is a
    # synthetic fixture, not a saved user session or a model execution result.
    return {"type": "system", "subtype": "init", "session_id": SESSION,
            "uuid": uid(), "cwd": "/tmp/fixture", "tools": [], "mcp_servers": [],
            "model": "fixture-model", "permissionMode": "default", "slash_commands": [],
            "apiKeySource": "none", "claude_code_version": "2.1.295",
            "output_style": "normal", "skills": [], "plugins": []}


def control(subtype="initialize", request_id="request-1", **fields):
    return {"type": "control_request", "request_id": request_id,
            "request": {"subtype": subtype, **fields}}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "owner.jsonl"
        self.source.touch()

    def append(self, *rows):
        with self.source.open("ab") as out:
            for row in rows:
                out.write(wire(row))

    def follower(self, fmt="transcript", history="replay", offset=None):
        binding = proxy.Binding(SESSION, self.source, fmt, "2.1.295", history, offset)
        follower = proxy.Follower(binding)
        self.addCleanup(follower.close)
        return follower

    def binding_file(self, **changes):
        data = {"schema": 1, "session_id": SESSION, "source": str(self.source),
                "source_format": "transcript", "claude_code_version": "2.1.295",
                "history": "native", "start_offset": 0}
        data.update(changes)
        path = self.root / "binding.json"
        path.write_text(json.dumps(data))
        return path

    def original_cli(self, version="2.1.295"):
        extension = self.root / "extension"
        binary = extension / "resources/native-binary/claude"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\ntouch '" + str(self.root / "ORIGINAL_WAS_EXECUTED") + "'\nexit 99\n")
        binary.chmod(0o755)
        (extension / "package.json").write_text(json.dumps({
            "name": "claude-code", "publisher": "Anthropic", "version": version,
        }))
        return binary

    def invocation(self, binary, session=SESSION):
        return [str(binary), "--output-format", "stream-json", "--verbose",
                "--input-format", "stream-json", "--resume", session]


class TailTests(Fixture):
    def test_fragmented_utf8_and_coalesced_records_keep_exact_boundaries(self):
        first, second = transcript(content="中文"), transcript(content="next")
        payload = wire(first)
        split = payload.index("中".encode("utf-8")) + 1
        self.source.write_bytes(payload[:split])
        tail = proxy.JsonlTail(self.source)
        self.addCleanup(tail.close)
        self.assertEqual(tail.read_available(), [])
        with self.source.open("ab") as out:
            out.write(payload[split:] + wire(second))
        records = tail.read_available()
        self.assertEqual(records, [(len(payload), first), (len(payload) + len(wire(second)), second)])
        self.assertEqual(tail.read_available(), [])

    def test_replaced_and_truncated_source_fail_without_reopening(self):
        self.append(transcript())
        tail = proxy.JsonlTail(self.source)
        self.addCleanup(tail.close)
        tail.read_available()
        self.source.write_bytes(b"")
        with self.assertRaisesRegex(proxy.ProtocolError, "truncated"):
            tail.read_available()
        self.source.unlink()
        self.source.write_bytes(wire(transcript()))
        with self.assertRaisesRegex(proxy.ProtocolError, "replaced"):
            tail.read_available()

    def test_symlink_and_non_json_sources_are_rejected(self):
        alias = self.root / "alias.jsonl"
        alias.symlink_to(self.source)
        with self.assertRaises(proxy.ProtocolError):
            proxy.JsonlTail(alias)
        self.source.write_bytes(b"not-json\n")
        with self.assertRaisesRegex(proxy.ProtocolError, "JSON"):
            self.follower()


class BindingTests(Fixture):
    def test_unknown_session_and_missing_or_unproven_source_do_not_create_anything(self):
        with self.assertRaisesRegex(proxy.ProtocolError, "does not prove"):
            self.follower()
        row = transcript()
        row["sessionId"] = OTHER
        self.append(row)
        with self.assertRaisesRegex(proxy.ProtocolError, "different session"):
            self.follower()
        self.source.unlink()
        with self.assertRaises(proxy.ProtocolError):
            self.follower()
        self.assertFalse(self.source.exists())

    def test_native_history_requires_an_explicit_complete_line_boundary(self):
        self.append(transcript())
        data = json.loads(self.binding_file().read_text())
        del data["start_offset"]
        path = self.root / "binding.json"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(proxy.ProtocolError, "implicit tail"):
            proxy.Binding.load(str(path))
        with self.assertRaisesRegex(proxy.ProtocolError, "boundary"):
            self.follower(history="native", offset=7)
        with self.assertRaisesRegex(proxy.ProtocolError, "boundary"):
            self.follower(history="native", offset=self.source.stat().st_size + 1)

    def test_exact_resume_and_pinned_extension_version(self):
        binding = proxy.Binding.load(str(self.binding_file()))
        binary = self.original_cli()
        proxy.validate_invocation(self.invocation(binary), binding)
        for args in [self.invocation(binary, OTHER), self.invocation(binary) + ["--continue"],
                     self.invocation(binary) + ["--fork-session"],
                     self.invocation(binary) + ["--resume", SESSION]]:
            with self.subTest(args=args), self.assertRaises(proxy.ProtocolError):
                proxy.validate_invocation(args, binding)
        self.original_cli(version="2.1.308")
        with self.assertRaisesRegex(proxy.ProtocolError, "2.1.295"):
            proxy.validate_invocation(self.invocation(binary), binding)
        self.assertFalse((self.root / "ORIGINAL_WAS_EXECUTED").exists())

    def test_no_resume_is_allowed_only_for_the_explicit_metadata_probe(self):
        binding = proxy.Binding.load(str(self.binding_file()))
        binary = self.original_cli()
        no_resume = self.invocation(binary)[:-2]
        with self.assertRaisesRegex(proxy.ProtocolError, "--resume"):
            proxy.validate_invocation(no_resume, binding)
        proxy.validate_invocation(no_resume, binding, config_probe=True)
        with self.assertRaisesRegex(proxy.ProtocolError, "must not select"):
            proxy.validate_invocation(self.invocation(binary), binding, config_probe=True)

    def test_producer_version_changes_fail_closed(self):
        row = transcript()
        self.append(row)
        follower = self.follower()
        follower.control(control())
        appended = transcript(parent=row["uuid"])
        appended["version"] = "2.1.308"
        self.append(appended)
        with self.assertRaisesRegex(proxy.ProtocolError, "producer version"):
            follower.poll()
        with self.assertRaisesRegex(proxy.ProtocolError, "producer version"):
            proxy.Binding.load(str(self.binding_file(claude_code_version="2.1.308")))

    def test_ready_receipt_requires_host_followup_and_never_overwrites(self):
        self.append(transcript())
        ready = self.root / "ready.json"
        binding = proxy.Binding.load(str(self.binding_file(ready_file=str(ready))))
        follower = proxy.Follower(binding)
        self.addCleanup(follower.close)
        follower.control(control("get_settings"))
        follower.write_ready()
        self.assertFalse(ready.exists())
        follower.control(control())
        follower.write_ready()
        self.assertFalse(ready.exists())
        follower.control(control("get_settings"))
        follower.write_ready()
        receipt = json.loads(ready.read_text())
        self.assertEqual(receipt["stage"], "host_initialize_observed")
        self.assertEqual(receipt["session_id"], SESSION)
        self.assertEqual(receipt["source_inode"], self.source.stat().st_ino)
        self.assertEqual(ready.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(proxy.ProtocolError, "must not already exist"):
            proxy.Binding.load(str(self.binding_file(ready_file=str(ready))))
        self.assertEqual(json.loads(ready.read_text()), receipt)


class FollowerTests(Fixture):
    def test_transcript_api_failure_is_preserved_without_a_completion_result(self):
        prompt = transcript()
        failure = transcript("assistant", [{"type": "text", "text": "API Error: 403 用户额度不足"}], prompt["uuid"])
        failure["message"]["model"] = "<synthetic>"
        failure.update({"isApiErrorMessage": True, "error": "authentication_failed", "apiErrorStatus": 403})
        self.append(prompt, failure)
        follower = self.follower()
        frames = follower.control(control())
        displayed = [f for f in frames if f["type"] == "assistant"]
        self.assertEqual(len(displayed), 1)
        self.assertEqual(displayed[0]["message"], failure["message"])
        self.assertFalse(any(f["type"] == "result" for f in frames))
        self.assertEqual(follower.poll(), [])

    def test_transcript_tool_call_result_and_user_content_are_preserved(self):
        prompt = transcript()
        call = transcript("assistant", [{"type": "tool_use", "id": "tool-1", "name": "Bash",
                                          "input": {"command": "printf fixture"}}], prompt["uuid"])
        result = transcript("user", [{"type": "tool_result", "tool_use_id": "tool-1",
                                      "content": "fixture", "is_error": False}], call["uuid"])
        sidechain = transcript("assistant", "hidden", result["uuid"])
        sidechain["isSidechain"] = True
        self.append(prompt, call, result, sidechain)
        frames = self.follower().control(control())
        visible = [f for f in frames if f["type"] in ("user", "assistant")]
        self.assertEqual([f["message"] for f in visible], [r["message"] for r in (prompt, call, result)])
        self.assertEqual([f["uuid"] for f in visible], [r["uuid"] for r in (prompt, call, result)])
        self.assertTrue(all(f["session_id"] == SESSION and f["parent_tool_use_id"] is None for f in visible))
        self.assertFalse(any(f["type"] == "result" for f in frames))

    def test_boundary_replays_attach_window_then_continues_without_duplicates(self):
        old = transcript()
        self.append(old)
        boundary = self.source.stat().st_size
        during_attach = transcript("assistant", "already appended", old["uuid"])
        self.append(during_attach)
        follower = self.follower(history="native", offset=boundary)
        before_initialize = transcript("user", "appended after process launch", during_attach["uuid"])
        self.append(before_initialize)
        initial = follower.control(control())
        self.assertEqual([f["uuid"] for f in initial if f["type"] == "assistant"], [during_attach["uuid"]])
        self.assertFalse(any(f.get("uuid") == old["uuid"] for f in initial))
        self.assertEqual([f["uuid"] for f in follower.poll()], [before_initialize["uuid"]])
        self.assertEqual(follower.poll(), [])
        self.append(old)
        self.assertEqual(follower.poll(), [])
        self.assertEqual(len(follower.control(control(request_id="again"))), 1)

    def test_real_sdk_frames_pass_unchanged_and_owner_rpc_is_not_forwarded(self):
        init = sdk_init()
        event = {"type": "stream_event", "uuid": uid(), "session_id": SESSION,
                 "parent_tool_use_id": None,
                 "event": {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": "真实流片段"}}}
        progress = {"type": "tool_progress", "uuid": uid(), "session_id": SESSION,
                    "tool_use_id": "tool-1", "tool_name": "Bash", "parent_tool_use_id": None,
                    "elapsed_time_seconds": 2}
        owner_rpc = control("can_use_tool", "owner-permission", tool_name="Bash", input={"command": "fixture"})
        self.append(init, event, owner_rpc, progress)
        follower = self.follower(fmt="stream-json")
        frames = follower.control(control(hooks={"PreToolUse": []}, plugins=[{"type": "local", "path": "/unused"}]))
        self.assertEqual(frames[1:-1], [init, event, progress])
        response = frames[0]["response"]["response"]
        self.assertEqual(response["current_model"], init["model"])
        self.assertEqual(response["current_permission_mode"], init["permissionMode"])
        self.assertFalse(response["hooks_applied"])
        self.assertFalse(response["plugins_applied"])
        self.assertFalse(any(f.get("type") == "control_request" for f in frames))

    def test_completion_is_only_forwarded_from_an_actual_source_result(self):
        init = sdk_init()
        self.append(init)
        follower = self.follower(fmt="stream-json")
        follower.control(control())
        self.assertEqual(follower.poll(), [])
        error_result = {"type": "result", "subtype": "error_during_execution", "is_error": True,
                        "uuid": uid(), "session_id": SESSION, "duration_ms": 8, "duration_api_ms": 0,
                        "num_turns": 0, "stop_reason": None, "total_cost_usd": 0,
                        "usage": {}, "modelUsage": {}, "permission_denials": [],
                        "errors": ["fixture owner failed"]}
        self.append(error_result)
        self.assertEqual(follower.poll(), [error_result])
        self.assertEqual(follower.poll(), [])

    def test_read_requests_and_mutations_fail_explicitly_without_success_or_side_effects(self):
        self.append(transcript())
        follower = self.follower()
        follower.control(control())
        before = self.source.read_bytes()
        for name in ("get_settings", "mcp_status", "set_model", "interrupt", "set_permission_mode",
                     "claim_session", "mcp_set_servers", "update_settings", "future_control"):
            with self.subTest(name=name):
                frames = follower.control(control(name, "correlate-" + name, model="must-not-apply"))
                self.assertEqual(len(frames), 1)
                response = frames[0]["response"]
                self.assertEqual(response["subtype"], "error")
                self.assertEqual(response["request_id"], "correlate-" + name)
                self.assertEqual(response["error_code"], "read_only_follower")
        self.assertEqual(self.source.read_bytes(), before)
        with self.assertRaisesRegex(proxy.ProtocolError, "refused a user message"):
            follower.control({"type": "user", "message": {"role": "user", "content": "must not execute"}})

    def test_duplicate_event_is_ignored_but_conflicting_or_foreign_event_stops(self):
        init = sdk_init()
        self.append(init)
        follower = self.follower(fmt="stream-json")
        follower.control(control())
        self.append(init)
        self.assertEqual(follower.poll(), [])
        changed = dict(init, model="another-model")
        self.append(changed)
        with self.assertRaisesRegex(proxy.ProtocolError, "UUID changed"):
            follower.poll()
        foreign = dict(init, session_id=OTHER, uuid=uid())
        with self.assertRaisesRegex(proxy.ProtocolError, "different session"):
            follower.accept(foreign)

    def test_split_assistant_content_is_supported_but_branch_rewrite_is_not(self):
        prompt = transcript()
        first = transcript("assistant", [{"type": "text", "text": "step"}], prompt["uuid"])
        second = transcript("assistant", [{"type": "tool_use", "id": "call", "name": "Bash", "input": {}}], prompt["uuid"])
        self.append(prompt, first, second)
        follower = self.follower()
        frames = follower.control(control())
        self.assertEqual(len([f for f in frames if f["type"] == "assistant"]), 2)
        branch = transcript("assistant", "alternative", prompt["uuid"], message_id="different-attempt")
        self.append(branch)
        with self.assertRaisesRegex(proxy.ProtocolError, "branch"):
            follower.poll()


class StdioTests(Fixture):
    def test_metadata_probe_cannot_replay_or_signal_viewer_ready(self):
        self.append(transcript())
        ready = self.root / "ready.json"
        binding = self.binding_file(ready_file=str(ready))
        ready.write_text("existing viewer receipt must survive")
        binary = self.original_cli()
        env = dict(os.environ, **{proxy.BINDING_ENV: str(binding), "CLAUDE_CODE_CONFIG_PROBE": "1"})
        result = subprocess.run([sys.executable, str(SCRIPT)] + self.invocation(binary)[:-2],
                                input=wire(control()) + wire(control("get_settings", "settings")),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.root,
                                timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f["type"] == "control_response" for f in frames))
        self.assertEqual(frames[0]["response"]["subtype"], "success")
        self.assertEqual(frames[1]["response"]["subtype"], "error")
        self.assertEqual(ready.read_text(), "existing viewer receipt must survive")
        self.assertFalse((self.root / "ORIGINAL_WAS_EXECUTED").exists())

    def test_real_python_wrapper_handshake_append_and_refusal_never_execute_original_cli(self):
        first = transcript()
        self.append(first)
        ready_file = self.root / "ready.json"
        binding = self.binding_file(ready_file=str(ready_file))
        binary = self.original_cli()
        env = dict(os.environ, **{proxy.BINDING_ENV: str(binding), "ANTHROPIC_BASE_URL": "https://fixture.invalid"})
        process = subprocess.Popen([sys.executable, str(SCRIPT)] + self.invocation(binary),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, cwd=self.root, bufsize=0)
        self.addCleanup(self.finish_process, process)
        buffer = b""

        def receive():
            nonlocal buffer
            while b"\n" not in buffer:
                ready, _, _ = select.select([process.stdout], [], [], 3)
                self.assertTrue(ready, "follower did not flush a frame")
                data = os.read(process.stdout.fileno(), 65536)
                self.assertTrue(data, "follower exited unexpectedly")
                buffer += data
            line, buffer = buffer.split(b"\n", 1)
            return json.loads(line)

        process.stdin.write(wire(control()))
        initial = [receive() for _ in range(3)]
        self.assertEqual(initial[0]["response"]["subtype"], "success")
        self.assertEqual(initial[1]["message"], first["message"])
        self.assertFalse(ready_file.exists())
        process.stdin.write(wire(control("get_settings", "settings")))
        self.assertEqual(receive()["response"]["subtype"], "error")
        deadline = time.monotonic() + 3
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(json.loads(ready_file.read_text())["stage"], "host_initialize_observed")
        appended = transcript("assistant", "new progress", first["uuid"])
        self.append(appended)
        self.assertEqual(receive()["message"], appended["message"])
        process.stdin.write(wire({"type": "user", "message": {"role": "user", "content": "do not run"}}))
        process.wait(timeout=3)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b"refused a user message", process.stderr.read())
        self.assertFalse((self.root / "ORIGINAL_WAS_EXECUTED").exists())
        self.assertEqual(self.source.read_bytes(), wire(first) + wire(appended))

    @staticmethod
    def finish_process(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


if __name__ == "__main__":
    unittest.main()
