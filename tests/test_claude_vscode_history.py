"""Exercise replay across native session models and follower channel restarts."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))
import claude_vscode_history as history
from claude_vscode_proxy import Binding, Follower, ProtocolError


# The relevant native receive-loop structure: rendering exceptions are skipped
# per frame, transport errors reach error.value, and EOF/restart clears process
# state while retaining the session's messages. UI/model internals are doubles.
NATIVE_SESSION = r'''class NativeSession {
constructor(id){
this.sessionId={value:id};this.messages=[];this.streamEvents=[];this.processed=[];
this.closed=[];this.skipped=[];this.error={value:null};this.renderAttempts=0;
this.connection={value:{closeChannel:id=>this.closed.push(id)}};
}
isSupersededChannel(){return false}
noteSendAcknowledgements(frame){this.acknowledged=(this.acknowledged??0)+1}
processIncomingMessage(frame){
this.renderAttempts++;
if(frame.message?.content==="fail once"&&!this.failedOnce){this.failedOnce=true;throw Error("Renderer failed")}
this.processed.push(frame);
if(frame.type==="user"||frame.type==="assistant")this.messages.push(frame);
if(frame.type==="stream_event")this.streamEvents.push(frame);
}
skipFailedMessage(frame,error){this.skipped.push(error.message);return true}
resetPerProcessState(){this.claudeChannelId=undefined}
flushPendingPromoEvents(){}
__READ_LOOP__
if(Y.type==="system"&&(Y.subtype==="init"||Y.subtype==="status"&&(Y.launchTiming!==void 0||Y.connectSnapshot===!0)))this.readyChannelId=J,this.flushPendingPromoEvents();
__COMMIT__
}}catch(Y){this.error.value=Y instanceof Error?Y.message:String(Y)}finally{this.resetPerProcessState()}}
}
'''.replace("__READ_LOOP__", history.WEBVIEW_READ_ANCHOR).replace(
    "__COMMIT__", history.WEBVIEW_COMMIT_ANCHOR).encode("utf-8")


class WebviewPatchValidationTests(unittest.TestCase):
    SESSION = "00000000-0000-4000-8000-000000000001"

    def test_unknown_webview_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Claude webview"):
            history.patch_webview(NATIVE_SESSION, self.SESSION)

    def test_even_a_recognized_build_requires_one_receive_loop(self):
        source = NATIVE_SESSION + NATIVE_SESSION
        with mock.patch.object(history, "WEBVIEW_SOURCE_SHA256", hashlib.sha256(source).hexdigest()):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                history.patch_webview(source, self.SESSION)

    def test_missing_commit_anchor_is_rejected(self):
        source = NATIVE_SESSION.replace(history.WEBVIEW_COMMIT_ANCHOR.encode(), b"missing")
        with mock.patch.object(history, "WEBVIEW_SOURCE_SHA256", hashlib.sha256(source).hexdigest()):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                history.patch_webview(source, self.SESSION)

    def test_session_id_is_validated_before_patching(self):
        with self.assertRaises(ProtocolError):
            history.patch_webview(NATIVE_SESSION, '";invalid-session')


@unittest.skipUnless(shutil.which("node"), "Node is needed to execute the native receive-loop patch")
class NativeReplayLifecycleTests(unittest.TestCase):
    SESSION = "00000000-0000-4000-8000-000000000001"
    OTHER = "00000000-0000-4000-8000-000000000002"
    USER = "00000000-0000-4000-8000-000000000011"
    ASSISTANT = "00000000-0000-4000-8000-000000000012"

    def run_js(self, body, data):
        with mock.patch.object(history, "WEBVIEW_SOURCE_SHA256", hashlib.sha256(NATIVE_SESSION).hexdigest()):
            source = history.patch_webview(NATIVE_SESSION, self.SESSION).decode("utf-8")
        program = (source + "\nconst sessionId=" + json.dumps(self.SESSION) + ";\n"
                   + "const data=" + json.dumps(data) + ";\n"
                   + "(async()=>{" + body + "})().catch(error=>{console.error(error);process.exit(1)});")
        result = subprocess.run([shutil.which("node"), "-e", program],
                                capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def assistant(self, text="Original reply", **extra):
        return {"type": "assistant", "session_id": self.SESSION, "uuid": self.ASSISTANT,
                "parent_tool_use_id": None,
                "message": {"role": "assistant", "id": "msg_a", "content": text}, **extra}

    def follower_replay(self, binding):
        follower = Follower(binding)
        try:
            frames = follower.control({"type": "control_request", "request_id": "init",
                                       "request": {"subtype": "initialize"}})
            return [frame for frame in frames if frame.get("type") in ("user", "assistant")]
        finally:
            follower.close()

    def test_fresh_follower_replay_and_new_appends_do_not_duplicate_native_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "owner.jsonl"
            rows = [
                {"type": "user", "sessionId": self.SESSION, "version": "2.1.295",
                 "uuid": self.USER, "parentUuid": None,
                 "message": {"role": "user", "content": "Task"}},
                {"type": "assistant", "sessionId": self.SESSION, "version": "2.1.295",
                 "uuid": self.ASSISTANT, "parentUuid": self.USER,
                 "message": {"role": "assistant", "id": "msg_a", "content": "Original reply"}},
            ]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            binding = Binding(self.SESSION, source, "transcript", "2.1.295", "replay")
            first = self.follower_replay(binding)
            second = self.follower_replay(binding)
            with source.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "type": "user", "sessionId": self.SESSION, "version": "2.1.295",
                    "uuid": "00000000-0000-4000-8000-000000000013", "parentUuid": self.ASSISTANT,
                    "message": {"role": "user", "content": "Follow-up"},
                }) + "\n")
            third = self.follower_replay(binding)
        actual = self.run_js('''const model=new NativeSession(sessionId),counts=[];
for(let i=0;i<data.length;i++){await model.readMessages(data[i],"channel-"+i);counts.push(model.messages.length)}
console.log(JSON.stringify({counts,uuids:model.messages.map(frame=>frame.uuid),closed:model.closed}));''',
                             [first, second, third])
        self.assertEqual(actual["counts"], [2, 2, 3])
        self.assertEqual(len(set(actual["uuids"])), 3)
        self.assertEqual(actual["closed"], [])

    def test_uuid_content_change_stops_only_that_viewer_before_later_frames(self):
        new = self.assistant("Unrelated later frame", uuid="00000000-0000-4000-8000-000000000013")
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages([data[0]],"first");
await model.readMessages([data[1],data[2]],"second");
console.log(JSON.stringify({texts:model.messages.map(frame=>frame.message.content),closed:model.closed,error:model.error.value,skipped:model.skipped}));''',
                             [self.assistant(), self.assistant("Changed reply"), new])
        self.assertEqual(actual["texts"], ["Original reply"])
        self.assertEqual(actual["closed"], ["second"])
        self.assertIn("existing event UUID changed", actual["error"])
        self.assertEqual(actual["skipped"], [])  # Reaches transport error, not the native skip handler.

    def test_json_object_key_order_is_not_a_content_change(self):
        original = self.assistant()
        reordered = dict(reversed(list(original.items())))
        reordered["message"] = dict(reversed(list(original["message"].items())))
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"same");
console.log(JSON.stringify({count:model.messages.length,error:model.error.value}));''',
                             [original, reordered])
        self.assertEqual(actual, {"count": 1, "error": None})

    def test_new_native_panels_each_get_their_own_complete_replay(self):
        actual = self.run_js('''const models=[new NativeSession(sessionId),new NativeSession(sessionId)];
for(const model of models)await model.readMessages(data,"panel");
console.log(JSON.stringify(models.map(model=>model.messages.length)));''', [self.assistant()])
        self.assertEqual(actual, [1, 1])

    def test_other_sessions_keep_native_behavior(self):
        frames = [self.assistant(session_id=self.OTHER), self.assistant("Changed", session_id=self.OTHER)]
        actual = self.run_js('''const model=new NativeSession(data[0].session_id);
await model.readMessages(data,"other");
console.log(JSON.stringify({count:model.messages.length,error:model.error.value,ledger:!!model.__claudexBridgeReplayFrames}));''', frames)
        self.assertEqual(actual, {"count": 2, "error": None, "ledger": False})

    def test_replayed_deltas_are_not_applied_twice_and_no_result_is_invented(self):
        frame = {"type": "stream_event", "session_id": self.SESSION, "uuid": self.ASSISTANT,
                 "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "text"}}}
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"first");await model.readMessages(data,"second");
console.log(JSON.stringify({deltas:model.streamEvents.length,types:model.processed.map(frame=>frame.type)}));''', [frame])
        self.assertEqual(actual, {"deltas": 1, "types": ["stream_event"]})

    def test_real_state_frames_restore_native_channel_state_on_reconnect(self):
        frames = [{"type": "system", "subtype": kind, "session_id": self.SESSION,
                   "uuid": "00000000-0000-4000-8000-{:012d}".format(index + 20)}
                  for index, kind in enumerate(("init", "task_started", "task_notification"))]
        frames.append({"type": "result", "session_id": self.SESSION,
                       "uuid": "00000000-0000-4000-8000-000000000023", "subtype": "success"})
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"first");await model.readMessages(data,"second");
console.log(JSON.stringify({processed:model.processed.length,ready:model.readyChannelId,error:model.error.value}));''', frames)
        self.assertEqual(actual, {"processed": 8, "ready": "second", "error": None})

    def test_state_frame_content_changes_are_still_rejected(self):
        first = {"type": "system", "subtype": "init", "session_id": self.SESSION,
                 "uuid": self.ASSISTANT, "model": "original"}
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"channel");
console.log(JSON.stringify({processed:model.processed.length,error:model.error.value}));''',
                             [first, {**first, "model": "changed"}])
        self.assertEqual(actual["processed"], 1)
        self.assertIn("existing event UUID changed", actual["error"])

    def test_failed_native_render_is_not_saved_as_already_displayed(self):
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"first");await model.readMessages(data,"second");
console.log(JSON.stringify({count:model.messages.length,attempts:model.renderAttempts,skipped:model.skipped}));''',
                             [self.assistant("fail once")])
        self.assertEqual(actual, {"count": 1, "attempts": 2, "skipped": ["Renderer failed"]})

    def test_wrong_frame_session_cannot_switch_bound_native_model(self):
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"channel");
console.log(JSON.stringify({count:model.messages.length,closed:model.closed,error:model.error.value}));''',
                             [self.assistant(session_id=self.OTHER)])
        self.assertEqual(actual["count"], 0)
        self.assertEqual(actual["closed"], ["channel"])
        self.assertIn("different session identity", actual["error"])


if __name__ == "__main__":
    unittest.main()
