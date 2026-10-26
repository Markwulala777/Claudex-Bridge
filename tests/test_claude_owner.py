"""Native owner control tests: private fixtures/Node VM only, never a model."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts/claude_owner.py"
spec = importlib.util.spec_from_file_location("claude_owner_fixture", SCRIPT)
owner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = owner
spec.loader.exec_module(owner)
check_execution_policy = owner._check_execution_policy
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.prepared = owner.prepare_plugin(self.state, SESSION)
        self.binding = owner._json(self.state / "binding.json")
        self.instance = self.binding["launch_id"] + "-fixture"
        self.sequence = 0
        self.event("ready", actual_session_id=SESSION)
        self.registry = self.root / "sessions"
        self.registry.mkdir(mode=0o700)
        self.native_path = self.root / "native.sock"
        self.native = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.native.close)
        self.native.bind(str(self.native_path))
        self.native_path.chmod(0o600)
        self.native.listen(4)
        self.native.settimeout(0.2)
        self.record_path = self.registry / (str(os.getpid()) + ".json")
        self.record = {"pid": os.getpid(), "sessionId": SESSION, "version": owner.VERSION,
                       "kind": "interactive", "peerProtocol": 1, "peerFeatures": ["notify_idle"],
                       "procStart": owner._start(os.getpid()), "status": "idle",
                       "messagingSocketPath": str(self.native_path)}
        owner._write(self.record_path, self.record)
        # Exercise the real launch-policy validator against a private /proc
        # fixture, retaining the actual PID/start/socket credential checks.
        self.policy_proc = self.root / "policy-proc"
        policy_pid = self.policy_proc / str(os.getpid())
        policy_pid.mkdir(parents=True)
        (policy_pid / "stat").symlink_to(Path("/proc") / str(os.getpid()) / "stat")
        self.environment = policy_pid / "environ"
        self.environment.write_bytes(b"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1\0")
        patcher = mock.patch.object(owner, "_check_execution_policy",
                                    side_effect=lambda current, proc_root="/proc":
                                    check_execution_policy(current, self.policy_proc))
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, kind, **extra):
        self.sequence += 1
        value = {"schema": 1, "kind": kind, "session_id": SESSION,
                 "claude_code_version": owner.VERSION, "launch_id": self.binding["launch_id"],
                 "instance_id": self.instance, "sequence": self.sequence, **extra}
        base = self.state / "events" / (self.instance + "-" + str(self.sequence).zfill(8))
        owner._write(base.with_suffix(".json"), value)
        base.with_suffix(".commit").write_text("1")
        return value

    def controller(self):
        result = owner.OwnerController(self.state, registry_dir=self.registry)
        self.addCleanup(result.close)
        return result

    def receive_native(self):
        connection, _ = self.native.accept()
        with connection:
            data = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data) if data else None

    def request(self, controller=None):
        controller = controller or self.controller()
        request_id = str(uuid.uuid4())
        state = controller.submit("implement fixture only", request_id)
        wire = self.receive_native()
        return controller, request_id, state, wire

    def test_prepare_is_private_exact_version_and_refuses_overwrite(self):
        self.assertEqual(owner.observe(self.state)["actual_session_id"], SESSION)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        plugin = Path(self.prepared["plugin_dir"])
        self.assertEqual(json.loads((plugin / "hooks/hooks.json").read_text()), {"modules": ["./bridge.js"]})
        self.assertNotIn("__CLAUDEX_OWNER_BINDING__", (plugin / "hooks/bridge.js").read_text())
        self.assertEqual(self.prepared["launch_env"], {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        with self.assertRaises(owner.OwnerError):
            owner.prepare_plugin(self.state, OTHER)

    def test_registry_rejects_wrong_version_recycled_pid_and_ambiguous_owner(self):
        self.assertEqual(owner.locate(SESSION, self.registry).pid, os.getpid())
        for changes in ({"version": "2.1.308"}, {"procStart": "stale"}, {"kind": "bg"}):
            owner._write(self.record_path, {**self.record, **changes})
            with self.subTest(changes=changes), self.assertRaises(owner.OwnerError):
                owner.locate(SESSION, self.registry)
        owner._write(self.record_path, self.record)
        owner._write(self.registry / "999999.json", self.record)
        with self.assertRaises(owner.OwnerError):
            owner.locate(SESSION, self.registry)

    def test_sends_exact_identity_once_and_never_converts_timeout_to_completion(self):
        controller, request_id, state, wire = self.request()
        self.assertEqual(wire["session_id"], SESSION)
        saved = owner._json(self.state / "requests" / (request_id + ".json"))
        self.assertEqual(wire["uuid"], saved["message_uuid"])
        self.assertEqual(wire["msg_id"], saved["msg_id"])
        self.assertTrue(wire["message"]["content"].startswith(saved["marker"] + "\n"))
        self.assertEqual(state["status"], "sent_unconfirmed")
        self.assertTrue(state["wait_timed_out"])
        self.assertNotIn("completion", state)
        for again in (request_id, str(uuid.uuid4())):
            with self.assertRaises(owner.OwnerError):
                controller.submit("must not resend", again)
        with self.assertRaises(socket.timeout):
            self.native.accept()

    def test_wrong_native_project_is_rejected_before_request_or_socket_send(self):
        with owner.OwnerController(self.state, registry_dir=self.registry,
                                   expected_project=self.root) as controller:
            with self.assertRaisesRegex(owner.OwnerError, "working directory"):
                controller.preflight()
            with self.assertRaisesRegex(owner.OwnerError, "working directory"):
                controller.submit("must stay in the bound project", str(uuid.uuid4()))
        self.assertEqual(list((self.state / "requests").iterdir()), [])
        with self.assertRaises(socket.timeout):
            self.native.accept()

    def test_project_is_rechecked_after_controller_preflight(self):
        original = Path.cwd()
        with owner.OwnerController(self.state, registry_dir=self.registry,
                                   expected_project=original) as controller:
            controller._open()
            try:
                os.chdir(self.root)
                with self.assertRaisesRegex(owner.OwnerError, "working directory"):
                    controller.submit("must not follow a changed cwd", str(uuid.uuid4()))
            finally:
                os.chdir(original)
        self.assertEqual(list((self.state / "requests").iterdir()), [])
        with self.assertRaises(socket.timeout):
            self.native.accept()

    def test_peer_credentials_checked_before_any_prompt_byte(self):
        controller = self.controller()
        with mock.patch.object(owner, "_peer", side_effect=owner.OwnerError("fixture wrong peer")):
            with self.assertRaises(owner.OwnerError):
                controller.submit("must not cross wrong socket", str(uuid.uuid4()))
        self.assertIsNone(self.receive_native())
        request = next((self.state / "requests").glob("*.json"))
        self.assertEqual(owner._json(request)["dispatch_status"], "prepared")

    def test_missing_or_ambiguous_owner_launch_policy_prevents_dispatch(self):
        for raw in (b"TOKEN=private-fixture-secret\0", b"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=0\0",
                    b"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1\0CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=0\0"):
            self.environment.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(owner.OwnerError) as raised:
                self.controller().submit("must not send", str(uuid.uuid4()))
            self.assertNotIn("private-fixture-secret", str(raised.exception))
        self.environment.unlink()
        with self.assertRaisesRegex(owner.OwnerError, "Cannot verify"):
            self.controller().preflight()
        self.assertEqual(list((self.state / "requests").glob("*.json")), [])
        with self.assertRaises(socket.timeout):
            self.native.accept()

    def test_launch_policy_is_rechecked_before_sending_after_preflight(self):
        controller = self.controller()
        controller._open()
        self.environment.write_bytes(b"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=0\0")
        with self.assertRaisesRegex(owner.OwnerError, "DISABLE_BACKGROUND_TASKS"):
            controller.submit("must not send", str(uuid.uuid4()))
        self.assertEqual(list((self.state / "requests").glob("*.json")), [])
        with self.assertRaises(socket.timeout):
            self.native.accept()

    def test_late_held_receipt_preserved_by_live_controller_and_other_request_ignored(self):
        controller, request_id, _, wire = self.request()
        for orig, status in ((str(uuid.uuid4()), "denied"), (wire["msg_id"], "held")):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(controller.reply_path))
                frame = {"type": "control", "action": "peer_message_status", "orig_msg_id": orig,
                         "from": "uds:" + str(self.native_path), "status": status, "reason": "fixture approval"}
                data = (json.dumps(frame) + "\n").encode()
                connection.sendall(data[:7])
                connection.sendall(data[7:])
        state = controller.observe(request_id)
        self.assertEqual(state["status"], "held")
        self.assertEqual(len(state["receipts"]), 1)
        self.assertNotIn("completion", state)

    def test_only_exact_native_turn_completion_is_terminal(self):
        controller, request_id, _, _ = self.request()
        self.event("turn_start", request_id=request_id, turn_id="native-turn")
        self.assertEqual(controller.observe(request_id)["status"], "running")
        self.event("permission_request", request_id=request_id, turn_id="native-turn", tool_name="Bash")
        self.assertEqual(controller.observe(request_id)["status"], "permission_wait")
        self.event("turn_complete", request_id=request_id, turn_id="native-turn", reason="error",
                   isAborted=False, answer="", durationMs=5)
        state = controller.observe(request_id, "native-turn")
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["completion"]["reason"], "error")
        self.assertEqual(state["completion"]["evidence"]["event_type"], "claude.turn.complete")
        self.assertEqual(state["completion"]["evidence"]["status"], "failed")
        with self.assertRaises(owner.OwnerError):
            controller.observe(request_id, "wrong-turn")

    def test_uncommitted_event_and_native_idle_cannot_finish_a_request(self):
        controller, request_id, _, _ = self.request()
        owner._write(self.state / "events/uncommitted.json", {"kind": "turn_complete"})
        self.assertEqual(controller.observe(request_id)["status"], "sent_unconfirmed")
        self.assertNotIn("completion", controller.observe(request_id))

    def test_parallel_permission_resolution_clears_wait_without_completing_turn(self):
        controller, request_id, _, _ = self.request()
        self.event("turn_start", request_id=request_id, turn_id="native-turn")
        for permission in ("first", "second"):
            self.event("permission_request", request_id=request_id, turn_id="native-turn",
                       permission_id=permission, prompt_id="shared-batch-dialog")
        self.event("permission_resolved", request_id=request_id, turn_id="native-turn", permission_id="first")
        self.assertEqual(controller.observe(request_id)["status"], "permission_wait")
        self.assertEqual(owner.view_state(self.state, SESSION)["waiting_for"], "permission")
        self.event("permission_resolved", request_id=request_id, turn_id="native-turn", permission_id="second")
        result = controller.observe(request_id)
        self.assertEqual(result["status"], "running")
        self.assertNotIn("completion", result)
        self.assertNotIn("waiting_for", owner.view_state(self.state, SESSION))
        self.event("permission_resolved", request_id=request_id, turn_id="native-turn", permission_id="unknown")
        with self.assertRaisesRegex(owner.OwnerError, "no matching request"):
            controller.observe(request_id)
        self.assertEqual(owner.view_state(self.state, SESSION)["state"], "unknown")

    def test_completion_without_matching_start_is_rejected(self):
        controller, request_id, _, _ = self.request()
        self.event("turn_complete", request_id=request_id, turn_id="unrelated", reason="answer", isAborted=False)
        with self.assertRaises(owner.OwnerError):
            controller.observe(request_id)

    def test_view_state_ready_requires_exact_session_and_single_instance(self):
        state = owner.view_state(self.state, SESSION)
        self.assertEqual(state["state"], "unknown")
        self.assertEqual(state["reason"], "ready_without_turn_evidence")
        self.assertEqual(owner.view_state(self.state, OTHER)["state"], "unknown")
        self.assertIn("binding", owner.view_state(self.state, OTHER)["reason"])
        self.instance += "-another"
        self.sequence = 0
        self.event("ready", actual_session_id=SESSION)
        self.assertEqual(owner.view_state(self.state, SESSION)["state"], "unknown")

    def test_view_state_running_is_live_and_completed_outcomes_use_native_evidence(self):
        controller = self.controller()
        for reason, expected in (("answer", "idle"), ("error", "failed"),
                                 ("refusal", "failed"), ("aborted", "stopped")):
            with self.subTest(reason=reason):
                _, request_id, _, _ = self.request(controller)
                self.assertEqual(owner.view_state(self.state, SESSION)["state"], "unknown")
                self.event("turn_start", request_id=request_id, turn_id="native-" + reason)
                state = owner.view_state(self.state, SESSION)
                self.assertEqual(state["state"], "running")
                self.assertTrue(state["owner_alive"])
                self.assertEqual(state["turn_id"], "native-" + reason)
                self.event("permission_request", request_id=request_id, turn_id="native-" + reason)
                self.assertEqual(owner.view_state(self.state, SESSION)["waiting_for"], "permission")
                self.event("turn_complete", request_id=request_id, turn_id="native-" + reason,
                           reason=reason, isAborted=reason == "aborted", answer="actual native outcome")
                state = owner.view_state(self.state, SESSION)
                self.assertEqual(state["state"], expected)
                self.assertEqual(state["reason"], reason)
                self.assertEqual(state["evidence"]["event_type"], "claude.turn.complete")
                self.assertEqual(state["evidence"]["status"],
                                 {"answer": "completed", "error": "failed", "refusal": "failed",
                                  "aborted": "interrupted"}[reason])
                self.assertEqual(state["evidence"]["execution_id"], "native-" + reason)
                self.assertNotIn("waiting_for", state)

    def test_view_state_never_reuses_completion_after_unclaimed_unknown_or_protocol_error(self):
        controller = self.controller()
        for kind in ("unclaimed_turn_start", "future_native_event", "protocol_error"):
            with self.subTest(kind=kind):
                _, request_id, _, _ = self.request(controller)
                self.event("turn_start", request_id=request_id, turn_id="native-" + kind)
                self.event("turn_complete", request_id=request_id, turn_id="native-" + kind,
                           reason="answer", isAborted=False, answer="real answer")
                self.assertEqual(owner.view_state(self.state, SESSION)["state"], "idle")
                self.event(kind, turn_id="unclaimed-later-turn")
                state = owner.view_state(self.state, SESSION)
                self.assertEqual(state["state"], "unknown")
                self.assertEqual(state["evidence"]["event_type"], kind)
        self.event("session_end", reason="exit")
        self.assertEqual(owner.view_state(self.state, SESSION)["state"], "stopped")

    def test_view_state_lost_owner_or_unmatched_completion_is_unknown(self):
        _, request_id, _, _ = self.request()
        self.event("turn_start", request_id=request_id, turn_id="native-turn")
        owner._write(self.record_path, {**self.record, "procStart": "recycled"})
        self.assertEqual(owner.view_state(self.state, SESSION)["state"], "unknown")
        owner._write(self.record_path, self.record)
        self.event("unclaimed_turn_start", turn_id="later-user-turn")
        self.event("turn_complete", request_id=request_id, turn_id="native-turn",
                   reason="answer", isAborted=False, answer="stale")
        self.assertEqual(owner.view_state(self.state, SESSION)["state"], "unknown")

    def test_interrupt_remains_available_while_dispatch_wait_lock_is_held(self):
        controller, request_id, _, _ = self.request()
        self.event("turn_start", request_id=request_id, turn_id="native-turn")
        with owner._lock(self.state):
            command = controller.interrupt(request_id, "native-turn")
        self.assertEqual(command["session_id"], SESSION)
        self.assertEqual(command["turn_id"], "native-turn")
        with self.assertRaises(owner.OwnerError):
            controller.interrupt(request_id, "stale-turn")
        with self.assertRaises(owner.OwnerError):
            controller.interrupt(request_id, "native-turn")  # no overwrite of an unanswered command
        self.event("turn_complete", request_id=request_id, turn_id="native-turn", reason="aborted", isAborted=True, answer="")
        self.assertEqual(controller.observe(request_id)["status"], "aborted")
        with self.assertRaises(owner.OwnerError):
            controller.interrupt(request_id, "native-turn")

    def test_owner_restart_is_unavailable_and_controller_closes_only_its_socket(self):
        controller, request_id, _, _ = self.request()
        reply_path = controller.reply_path
        owner._write(self.record_path, {**self.record, "procStart": "recycled"})
        self.assertEqual(controller.observe(request_id)["status"], "unavailable")
        controller.close()
        self.assertFalse(reply_path.exists())
        self.assertTrue(self.native_path.exists())

    def test_fragmented_receipt_survives_multiple_observe_calls(self):
        controller, request_id, _, wire = self.request()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(str(controller.reply_path))
        frame = {"type": "control", "action": "peer_message_status", "orig_msg_id": wire["msg_id"],
                 "from": "uds:" + str(self.native_path), "status": "held"}
        data = (json.dumps(frame) + "\n").encode()
        connection.sendall(data[:13])
        self.assertEqual(controller.observe(request_id)["receipts"], [])
        connection.sendall(data[13:])
        self.assertEqual(controller.observe(request_id)["status"], "held")

    def test_actual_peer_credential_validator_rejects_wrong_pid_or_uid(self):
        bound = owner.locate(SESSION, self.registry)
        for pid, uid in ((bound.pid + 1, bound.uid), (bound.pid, bound.uid + 1)):
            connection = mock.Mock()
            connection.getsockopt.return_value = owner.struct.pack("3i", pid, uid, 0)
            with self.subTest(pid=pid, uid=uid), self.assertRaises(owner.OwnerError):
                owner._peer(connection, bound)

    def test_partial_send_failure_is_durable_unknown_and_cannot_be_retried(self):
        controller = self.controller()
        controller._open()
        real_factory = controller.socket_factory
        class PartialSocket:
            def __init__(self, *args):
                self.socket = real_factory(*args)
            def __getattr__(self, name):
                return getattr(self.socket, name)
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.socket.close()
            def sendall(self, data):
                self.socket.sendall(data[:5])
                raise OSError("fixture ambiguous partial delivery")
        controller.socket_factory = PartialSocket
        request_id = str(uuid.uuid4())
        with self.assertRaises(OSError):
            controller.submit("fixture", request_id)
        with self.native.accept()[0] as connection:
            self.assertEqual(connection.recv(100), b'{"typ')
        self.assertEqual(controller.observe(request_id)["status"], "sending_unknown")
        with self.assertRaises(owner.OwnerError):
            controller.submit("fixture", request_id)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the actual hook module offline")
    def test_actual_native_module_identity_lifecycle_and_precise_abort(self):
        module = Path(self.prepared["plugin_dir"]) / "hooks/bridge.js"
        harness = r'''
const fs = require('fs'); const vm = require('vm'); const assert = require('assert/strict');
const [moduleFile, stateDir, bindingText] = process.argv.slice(1);
const binding = JSON.parse(bindingText), files = new Map(), hooks = new Map();
let clock, aborts = [], currentSession = binding.session_id;
const $ = {
 session: {id:async()=>currentSession, version:async()=>({version:'2.1.295'})},
 fs: {exists:async p=>files.has(p),read:async p=>files.get(p),write:async(p,v)=>{files.set(p,v)}},
 clock:{every:(ms,fn)=>{clock=fn;return{cancel(){}}}},
 turn:{abort:async({turnId})=>{aborts.push(turnId)}}
};
vm.runInNewContext(fs.readFileSync(moduleFile,'utf8').replace('export function register','function register')+';register(on);',
 {on:(name,fn)=>hooks.set(name,fn)});
const emit=(name,e)=>hooks.get(name)($,e,async value=>value);
const events=()=>[...files].filter(([p])=>p.endsWith('.commit')).map(([p])=>JSON.parse(files.get(p.replace(/\.commit$/,'.json'))));
(async()=>{
 await emit('session.start',{});
 const ready=events()[0]; assert.equal(ready.kind,'ready'); assert.equal(ready.actual_session_id,binding.session_id);
 const request={schema:1, session_id:binding.session_id, launch_id:binding.launch_id, instance_id:ready.instance_id,
  request_id:'request-one',marker:'[claudex-request:request-one]'};
 files.set(stateDir+'/active.json',JSON.stringify(request));
 await emit('turn.start',{agentId:'child',turnId:'child-turn',text:request.marker+'\nbody'});
 assert.equal(events().length,1);
 await emit('turn.start',{turnId:'human-turn',text:'unrelated input'});
 assert.equal(events().at(-1).kind,'unclaimed_turn_start');
 await emit('turn.start',{turnId:'exact-turn',text:request.marker+'\nbody'});
 assert.equal(events().at(-1).turn_id,'exact-turn');
 const cmd={schema:1, command_id:'command-1',session_id:binding.session_id,launch_id:binding.launch_id,
  instance_id:ready.instance_id,request_id:'request-one',turn_id:'stale-turn'};
 files.set(stateDir+'/interrupt.json',JSON.stringify(cmd)); await clock();
 assert.deepEqual(aborts,[]); assert.equal(events().at(-1).kind,'interrupt_rejected');
 files.set(stateDir+'/interrupt.json',JSON.stringify({...cmd,command_id:'command-2',turn_id:'exact-turn'}));
 await clock(); await clock(); assert.deepEqual(aborts,['exact-turn']);
 await emit('classic.PermissionRequest',{session_id:binding.session_id,tool_name:'Bash'});
 assert.equal(events().at(-1).kind,'permission_resolved');
 const permissionEvents=events().filter(e=>e.kind.startsWith('permission_'));
 assert.equal(permissionEvents.length,2);
 assert.equal(permissionEvents[0].permission_id,permissionEvents[1].permission_id);
 await emit('turn.complete',{agentId:'child',turnId:'exact-turn',reason:'answer',isAborted:false,answer:'wrong child'});
 await emit('turn.complete',{turnId:'unrelated',reason:'answer',isAborted:false,answer:'wrong turn'});
 assert.equal(events().filter(e=>e.kind==='turn_complete').length,0);
 await emit('turn.complete',{turnId:'exact-turn',reason:'aborted',isAborted:true,answer:'actual native text',durationMs:4});
 const done=events().at(-1);assert.equal(done.kind,'turn_complete');assert.equal(done.reason,'aborted');
 assert.equal(done.answer,'actual native text');assert.equal(done.request_id,'request-one');
 await emit('turn.complete',{turnId:'exact-turn',reason:'answer',isAborted:false,answer:'duplicate'});
 assert.equal(events().filter(e=>e.kind==='turn_complete').length,1);
 assert.deepEqual(events().map(e=>e.sequence),events().map((_,i)=>i+1));
 currentSession='wrong-session';
 await assert.rejects(()=>emit('turn.start',{turnId:'another',text:request.marker+'\nbody'}),/changed/);
 console.log('native lifecycle invariants passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run(["node", "-e", harness, str(module), str(self.state), json.dumps(self.binding)],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("invariants passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
