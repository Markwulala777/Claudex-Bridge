"""Version-pinned, isolated extension patch for one history/event reader.

The official panel otherwise loads saved messages before launching its SDK
transport. A live owner can append between those reads. For the bound session
only, the proxy replays history and follows the same open source descriptor;
the panel keeps the native diff metadata but receives messages from that stream.
The matching webview patch retains its per-session replay identities across a
follower reconnect. Native restartClaude keeps existing messages, and its
assistant UUID merge only runs after a real stream_event. Transcript followers
must not fabricate such an event merely to turn that merge on.

No installed extension, authentication, or owner execution-control code changes.
"""
import hashlib
import json

from claude_vscode_proxy import require_uuid


SOURCE_SHA256 = "23f91879a93db01805307472aeb648ebd0b4b06ee625aa6cfe5dca724ae1c902"
ANCHOR = 'messages:this.stampReplayedFrames([...z,...V]),sessionDiffs:G,liveInOtherSurface:this.isSessionLiveInOtherSurface($)'
WEBVIEW_SOURCE_SHA256 = "0f11dd8dbf644aa03d46d3d91b3edbb78dcd35eddd389e2b0cb7507ba4008eb9"
WEBVIEW_READ_ANCHOR = 'async readMessages($,J){let Z=0,X=!1;try{for await(let Y of $){if(this.isSupersededChannel(J))continue;try{'
WEBVIEW_COMMIT_ANCHOR = 'this.noteSendAcknowledgements(Y),this.processIncomingMessage(Y)}catch(Q){if(Z+=1,this.skipFailedMessage(Y,Q,Z,!X))X=!0}'


# Keep the identity ledger on the native session model, not the temporary SDK
# channel. A different panel/model receives its own complete replay. Compare the
# full canonical JSON, so a UUID collision never silently hides changed content.
# Store only successfully processed frames; a native rendering failure must not
# cause a later retry to be mistaken for a previously displayed message.
_WEBVIEW_HELPER = """function __claudexBridgeReplayCheck(model,frame,sessionId){
if(typeof frame?.uuid!=="string")return null;
if(frame.session_id!==sessionId)throw Error("Claudex Bridge stopped this viewer: an event has a different session identity.");
const canonical=value=>value===null||typeof value!=="object"?JSON.stringify(value):Array.isArray(value)?"["+value.map(canonical).join(",")+"]":"{"+Object.keys(value).sort().map(key=>JSON.stringify(key)+":"+canonical(value[key])).join(",")+"}";
const signature=canonical(frame),frames=model.__claudexBridgeReplayFrames??=new Map;
if(frames.has(frame.uuid)){
if(frames.get(frame.uuid)!==signature)throw Error("Claudex Bridge stopped this viewer: an existing event UUID changed. Recreate the view from a verified source.");
if(frame.type==="system"&&["init","status","task_started","task_progress","task_notification","task_updated","background_tasks_changed"].includes(frame.subtype)||["result","auth_status","rate_limit_event","command_lifecycle","prompt_suggestion","tool_progress","tool_use_summary"].includes(frame.type))return null;
return false;
}
return {frames,key:frame.uuid,signature};
}
"""


def patch(source, session_id):
    require_uuid(session_id, "session_id")
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unsupported Claude extension source; refusing an unverified history patch")
    text = source.decode("utf-8")
    if text.count(ANCHOR) != 1:
        raise ValueError("Expected exactly one native history response")
    replacement = ('messages:($===' + json.dumps(session_id) +
                   '?[]:this.stampReplayedFrames([...z,...V])),sessionDiffs:G,'
                   'liveInOtherSurface:this.isSessionLiveInOtherSurface($)')
    return text.replace(ANCHOR, replacement, 1).encode("utf-8")


def patch_webview(source, session_id):
    """Patch only the verified native receive loop for one bound session.

    The replay check runs outside the native per-message catch: an identity
    conflict closes this follower channel and reaches the native stream error
    display, instead of becoming a silently skipped rendering error. It neither
    interrupts the executing owner nor emits SDK messages or successful results.
    """
    require_uuid(session_id, "session_id")
    if hashlib.sha256(source).hexdigest() != WEBVIEW_SOURCE_SHA256:
        raise ValueError("Unsupported Claude webview source; refusing an unverified replay patch")
    text = source.decode("utf-8")
    if text.count(WEBVIEW_READ_ANCHOR) != 1 or text.count(WEBVIEW_COMMIT_ANCHOR) != 1:
        raise ValueError("Expected exactly one native session receive loop")
    bound = json.dumps(session_id)
    check = (
        'let claudexBridgeReplayFrame;'
        'if(this.sessionId.value===' + bound + '){'
        'try{claudexBridgeReplayFrame=__claudexBridgeReplayCheck(this,Y,' + bound + ');'
        'if(claudexBridgeReplayFrame===false)continue;}'
        'catch(claudexBridgeReplayError){'
        'try{this.connection.value?.closeChannel(J);}finally{throw claudexBridgeReplayError;}}}'
        'try{'
    )
    text = text.replace(WEBVIEW_READ_ANCHOR, WEBVIEW_READ_ANCHOR[:-4] + check, 1)
    commit = (
        'this.noteSendAcknowledgements(Y),this.processIncomingMessage(Y);'
        'if(claudexBridgeReplayFrame)claudexBridgeReplayFrame.frames.set('
        'claudexBridgeReplayFrame.key,claudexBridgeReplayFrame.signature);'
        '}catch(Q){if(Z+=1,this.skipFailedMessage(Y,Q,Z,!X))X=!0}'
    )
    text = text.replace(WEBVIEW_COMMIT_ANCHOR, commit, 1)
    return (_WEBVIEW_HELPER + text).encode("utf-8")
