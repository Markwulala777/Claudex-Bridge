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


# Portable UI doubles retain the pinned native constructor/filter/map anchors.
# The installed-source test below also executes the actual QH/Bv0/IS/eD0 bodies.
NATIVE_PEER_UI = r'''
class m8 {constructor(content){this.content=content}}
class xX {constructor(type,content,options){this.type=type;this.content=content;Object.assign(this,options)}}
const rB="synthetic"; const kw=value=>value;
function QH($){
let J=false,Z,X=false,Y=$.parent_tool_use_id,Q=Y,z=$.isSynthetic,G=$.origin,q=false,V=false;
let W=typeof $.message.content==="string"?[new m8({type:"text",text:$.message.content})]:$.message.content.map(c=>new m8(c));
__CREATE__
}
function Bv0($){return false}
function SI($){return Bv0($)}
function G$(content){return {type:"text",text:content.text}}
function zD0(row,content){return G$(content)}
function QD5({message:Z,readOnly:V}){let K=[...Z.content].reverse(),A=K.map((y)=>V?zD0(Z,y.content):G$(y.content));return A}
function IS($,J,Z,X,Y,Q,z,G,q,V){let U=V?.readOnly??false;
if(J.isSynthetic&&(U||!SI(J)))return null;
return QD5({message:J,readOnly:U});}
'''.replace("__CREATE__", history.WEBVIEW_PEER_CREATE_ANCHOR)


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
    "__COMMIT__", history.WEBVIEW_COMMIT_ANCHOR)
NATIVE_SESSION = (NATIVE_SESSION + NATIVE_PEER_UI).encode("utf-8")


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

    def test_each_native_peer_anchor_must_exist_exactly_once(self):
        for anchor in (history.WEBVIEW_PEER_CREATE_ANCHOR, history.WEBVIEW_PEER_CLASSIFY_ANCHOR,
                       history.WEBVIEW_PEER_FILTER_ANCHOR, history.WEBVIEW_PEER_CONTENT_ANCHOR):
            for source in (NATIVE_SESSION.replace(anchor.encode(), b"missing"),
                           NATIVE_SESSION + anchor.encode()):
                with self.subTest(anchor=anchor[:35]), mock.patch.object(
                        history, "WEBVIEW_SOURCE_SHA256", hashlib.sha256(source).hexdigest()):
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

    def test_only_exact_bound_peer_objects_use_native_peer_ui_without_content_mutation(self):
        body = "Another Claude session sent a message:\n[claudex-request:fixture]\n真实派工正文\n<literal>"
        peer = {"type": "user", "session_id": self.SESSION, "uuid": self.USER,
                "isSynthetic": True, "parent_tool_use_id": None,
                "origin": {"kind": "peer", "from": "uds:/fixture.sock", "verifiedPeerPid": 123},
                "message": {"role": "user", "content": body}}
        nonpeer = {**peer, "uuid": self.ASSISTANT, "origin": {"kind": "plugin"}}
        subagent = {**peer, "uuid": "00000000-0000-4000-8000-000000000013",
                    "origin": {**peer["origin"], "senderTaskId": "child"}}
        result = self.run_js('''const before=JSON.stringify(data),model=new NativeSession(sessionId);
await model.readMessages(data,"bound");
const rows=data.map(frame=>QH(frame));
const other=JSON.parse(JSON.stringify(data[0]));other.session_id="''' + self.OTHER + '''";
const otherModel=new NativeSession(other.session_id);await otherModel.readMessages([other],"other");
const otherRow=QH(other),sameIdUnboundRow=QH(JSON.parse(JSON.stringify(data[0])));
const render=(row,readOnly)=>IS(model,row,"row",{},false,null,null,null,null,{readOnly});
console.log(JSON.stringify({normal:render(rows[0],false),readOnly:render(rows[0],true),
hidden:[rows[1],rows[2],otherRow,sameIdUnboundRow].map(row=>render(row,false)),
sameText:rows[0].content[0].content.text===data[0].message.content,
sameOrigin:rows[0].origin===data[0].origin,synthetic:rows[0].isSynthetic,unchanged:before===JSON.stringify(data)}));''',
                             [peer, nonpeer, subagent])
        for mode in ("normal", "readOnly"):
            self.assertEqual(result[mode][0]["type"], "peerMessage")
            self.assertEqual(result[mode][0]["message"]["kind"], "cross-session")
            self.assertEqual(result[mode][0]["message"]["body"], body)
            self.assertEqual(result[mode][0]["originalText"], body)
        self.assertEqual(result["hidden"], [None, None, None, None])
        self.assertTrue(all(result[key] for key in ("sameText", "sameOrigin", "synthetic", "unchanged")))

    def test_wrong_frame_session_cannot_switch_bound_native_model(self):
        actual = self.run_js('''const model=new NativeSession(sessionId);
await model.readMessages(data,"channel");
console.log(JSON.stringify({count:model.messages.length,closed:model.closed,error:model.error.value}));''',
                             [self.assistant(session_id=self.OTHER)])
        self.assertEqual(actual["count"], 0)
        self.assertEqual(actual["closed"], ["channel"])
        self.assertIn("different session identity", actual["error"])


@unittest.skipUnless(shutil.which("node"), "Node is needed to execute installed native UI functions")
class InstalledNativePeerUITests(unittest.TestCase):
    def test_installed_pinned_functions_render_real_peer_text_and_keep_other_rows_hidden(self):
        candidates = (Path.home() / ".vscode/extensions").glob(
            "anthropic.claude-code-2.1.295-*/webview/index.js")
        source_path = next((path for path in candidates
                            if hashlib.sha256(path.read_bytes()).hexdigest() == history.WEBVIEW_SOURCE_SHA256), None)
        if source_path is None:
            self.skipTest("Pinned official 2.1.295 webview is not installed")
        session_id = "00000000-0000-4000-8000-000000000001"
        patched = history.patch_webview(source_path.read_bytes(), session_id).decode("utf-8")
        harness = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const data=JSON.parse(fs.readFileSync(0,'utf8')),original=fs.readFileSync(data.path,'utf8');
new vm.Script(data.patched); // Parse the full patched official bundle without running the application.
function cut(source,start,end){
 const a=source.indexOf(start),b=source.indexOf(end,a+start.length);
 assert(a>=0&&b>a,'missing installed function boundary '+start);return source.slice(a,b);
}
function check(source,patched){
 const mapStart='A=K.map((y)=>',mapAt=source.indexOf(mapStart,source.indexOf('QD5=Rq(function'));
 const mapping=source.slice(mapAt,source.indexOf(',j=""',mapAt));
 assert(mapping.startsWith(mapStart));
 const modelCode=[
 'const UI="agent-message",pA="cross-session-message",Ew="channel";',
 'class m8{constructor(content){this.content=content}}',
 'class xX{constructor(type,content,options){this.type=type;this.content=content;Object.assign(this,options)}}',
 'const P0=value=>value,kw=value=>value,rB="synthetic";',
 'const x=()=>[false,()=>{}],zy1=v=>v,K91=v=>v,F91=(v,n)=>v.slice(0,n),sx=80,lQ=v=>v,iq="chevron",_3={};',
 'const F=(type,props)=>typeof type==="function"?type(props):({type,props}),R=F;',
 'function G$(content){return {type:"text",text:content.text}}function zD0(row,content){return G$(content)}',
 cut(source,'var rv1=','var eI0='),
 cut(source,'var ig=','var $y1='),
 cut(source,'function Cw(','var hk0='),
 cut(source,'function QH(','function dv1('),
 cut(source,'function SI(','function D31('),
 cut(source,'function Bv0(','function Kv0('),
 cut(source,'function IS(','function GD5('),
 cut(source,'function eD0(','function $A0('),
 'function QD5({message:Z,readOnly:V}){let K=[...Z.content].reverse(),'+mapping+
 ';return A.map(e=>e.type==="peerMessage"&&Cw(Z.origin,e.message)?eD0({message:e.message,originName:Z.origin.name,onToggle:()=>{}}):e)}',
 ].join('\n');
 const assertions=String.raw`
 const body="Another Claude session sent a message:\n[claudex-request:fixture]\n真实正文 <literal>\n\nOriginal native notice.";
 const frame={type:"user",session_id:sessionId,uuid:"peer-row",parent_tool_use_id:null,isSynthetic:true,
  origin:{kind:"peer",from:"uds:/fixture.sock",verifiedPeerPid:123},message:{role:"user",content:body}};
 const before=JSON.stringify(frame),model={sessionId:{value:sessionId},pastedContentId:{value:null},cwd:{value:"/fixture"}};
 __claudexBridgeReplayCheck(model,frame,sessionId);
 const row=QH(frame),render=(r,readOnly)=>IS(model,r,"row",{},false,null,null,null,null,{readOnly});
 assert.equal(xw(body),null); // The actual pinned parser rejects the TUI's legacy envelope.
 if(patched){
   assert.equal(SI(row),true);
   for(const readOnly of [false,true]){
     const tree=render(row,readOnly);assert.equal(tree[0].type,"details");
     const children=tree[0].props.children;
     assert.equal(children.at(-1).props.children,body);
     assert.equal(children[0].props.children[0].props.children,"Message from @peer:");
   }
 }else{assert.equal(SI(row),false);assert.equal(render(row,false),null);assert.equal(render(row,true),null)}
 assert.equal(row.isSynthetic,true);assert.equal(row.origin,frame.origin);assert.equal(JSON.stringify(frame),before);
 const other=JSON.parse(before);other.session_id="00000000-0000-4000-8000-000000000002";
 assert.equal(render(QH(other),false),null); // Same UUID/body in another model is not enrolled.
 const plugin={...frame,uuid:"plugin-row",origin:{kind:"plugin"}};
 __claudexBridgeReplayCheck(model,plugin,sessionId);assert.equal(render(QH(plugin),false),null);
 const child={...frame,uuid:"child-row",origin:{kind:"peer",senderTaskId:"child"}};
 __claudexBridgeReplayCheck(model,child,sessionId);assert.equal(render(QH(child),false),null);
 `;
 vm.runInNewContext(data.helper+'\n'+modelCode+'\n'+assertions,
   {assert,sessionId:data.session,patched},{timeout:2000});
}
check(original,false);check(data.patched,true);
console.log(JSON.stringify({originalHidden:true,patchedNativePeer:true,bodyPreserved:true,otherRowsHidden:true}));
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            input=json.dumps({"path": str(source_path), "patched": patched,
                              "helper": history._WEBVIEW_HELPER, "session": session_id}),
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "originalHidden": True, "patchedNativePeer": True,
            "bodyPreserved": True, "otherRowsHidden": True})


if __name__ == "__main__":
    unittest.main()
