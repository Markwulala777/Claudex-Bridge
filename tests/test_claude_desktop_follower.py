"""Offline tests: no Electron, IPC, model, user transcript, or credential reads."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "skills/claudex-bridge/scripts/claude_desktop_follower.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required to test the Electron-side adapter")
class NativeDesktopFollowerTests(unittest.TestCase):
    def run_js(self, body):
        script = """
const assert = require('node:assert/strict');
const {NativeDesktopFollower, createLocalSessionsApi, makeListenerPatches, FollowerError} = require(MODULE);
const UUID = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const binding = {
  schema: 1, desktop_version: '3.6556.1', claude_code_version: '2.1.295',
  session_id: UUID, cwd: '/tmp/claudex-offline-fixture', source_identity: 'fixture-inode-1',
  owner_state: 'running', model: 'fixture-model', effort: 'high',
};
const row = (id, text = 'fixture content') => ({
  type: 'assistant', session_id: UUID, uuid: id,
  message: {role: 'assistant', content: [{type: 'text', text}]},
});
const batch = (sequence, messages, extra = {}) => ({
  sequence, messages, session_id: UUID, source_identity: binding.source_identity,
  claude_code_version: binding.claude_code_version, ...extra,
});
const fails = (fn, code) => assert.throws(fn, e => e instanceof FollowerError && e.code === code);
const follower = new NativeDesktopFollower(binding);
const api = createLocalSessionsApi(follower);
const events = [];
follower.subscribe(e => events.push(e));
""".replace("MODULE", json.dumps(str(MODULE))) + body
        result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_binding_and_source_fail_closed(self):
        self.run_js("""
fails(() => new NativeDesktopFollower({...binding, desktop_version: 'future'}), 'version');
fails(() => new NativeDesktopFollower({...binding, claude_code_version: '2.1.308'}), 'version');
fails(() => new NativeDesktopFollower({...binding, owner_state: 'invented'}), 'binding');
fails(() => follower.ingest(batch(0, [row('a')], {source_identity: 'rotated'})), 'source');
fails(() => follower.ingest(batch(0, [row('a')], {session_id: OTHER})), 'session');
fails(() => follower.ingest(batch(0, [row('a')], {claude_code_version: 'future'})), 'version');
assert.deepEqual(api.getTranscript(follower.sessionId), []);
assert.equal(follower.nextSequence, 0);
""")

    def test_transcript_only_follower_does_not_claim_execution_phase(self):
        self.run_js("""
const unobserved = new NativeDesktopFollower({...binding, owner_state: undefined});
unobserved.ingest(batch(0, [row('assistant')]));
const view = unobserved.view();
assert.equal(unobserved.ownerState, 'unknown');
assert.equal(view.turnRunning, undefined);
assert.match(view.footerIndicator, /unknown/);
assert.equal(typeof view.isRunning, 'boolean');
""")

    def test_whole_batch_is_validated_before_delivery(self):
        self.run_js("""
follower.announce();
events.length = 0;
fails(() => follower.ingest(batch(0, [row('valid'), {...row('foreign'), session_id: OTHER}])), 'session');
assert.equal(follower.nextSequence, 0);
assert.deepEqual(events, []);
assert.deepEqual(api.getTranscript(follower.sessionId), []);
follower.ingest(batch(0, [row('valid')]));
fails(() => follower.ingest(batch(0, [row('valid')])), 'sequence');
fails(() => follower.ingest(batch(2, [row('gap')])), 'sequence');
assert.equal(events.length, 1);
assert.equal(api.getTranscript(follower.sessionId).length, 1);
""")

    def test_native_history_then_incremental_events_without_fake_init(self):
        self.run_js("""
follower.ingest(batch(0, [row('history')]));
assert.deepEqual(events, []);
follower.announce();
assert.deepEqual(events.map(e => e.type), ['start']);
const delta = {type: 'stream_event', session_id: UUID, event: {type: 'content_block_delta', delta: {type: 'text_delta', text: 'new'}}};
follower.ingest(batch(1, [delta, row('new', 'new')]));
assert.deepEqual(events.slice(1).map(e => e.message), [delta, row('new', 'new')]);
assert.deepEqual(api.getTranscript(follower.sessionId).map(m => m.uuid), ['history', 'new']);
assert.equal(events.some(e => e.message?.type === 'result' || e.message?.subtype === 'init'), false);
assert.equal(api.getSession(follower.sessionId).model, 'fixture-model');
assert.equal(api.getSession(follower.sessionId).effort, 'high');
""")

    def test_actual_failure_is_preserved_and_eof_does_not_mean_success(self):
        self.run_js("""
follower.announce();
const failure = {...row('failure', 'API Error: 403 quota exhausted'), error: 'unknown'};
follower.ingest(batch(0, [failure]));
assert.deepEqual(events.at(-1).message, failure);
assert.equal(api.getSession(follower.sessionId).isRunning, true);
follower.ingest(batch(1, []));
assert.equal(api.getSession(follower.sessionId).isRunning, true);
follower.ingest(batch(1, [], {owner_state: 'failed'}));
assert.equal(api.getSession(follower.sessionId).isRunning, false);
assert.equal(events.at(-1).type, 'session_updated');
follower.sourceUnavailable('source stream interrupted');
assert.equal(api.getSession(follower.sessionId).transcriptUnavailable, true);
assert.equal(api.getSession(follower.sessionId).isRunning, false);
assert.equal(events.some(e => e.message?.type === 'result'), false);
fails(() => follower.ingest(batch(1, [row('late')])), 'source');
""")

    def test_paging_preserves_native_ticket_and_exact_session(self):
        self.run_js("""
follower.ingest(batch(0, ['a','b','c','d'].map(id => row(id))));
assert.deepEqual(api.getTranscriptTail(follower.sessionId, 2).messages.map(m => m.uuid), ['c','d']);
const page = api.readTranscript({sessionId: follower.sessionId, mode: 'page', beforeUuid: 'd', limit: 2, ticket: 53});
assert.equal(page.ticket, 53);
assert.equal(page.via, 'inline');
assert.deepEqual(page.messages.map(m => m.uuid), ['b','c']);
assert.equal(page.hasMore, true);
assert.equal(api.getTranscriptPage(follower.sessionId, 'missing', 5).found, false);
assert.equal(api.getSession(`local_${OTHER}`), null);
fails(() => api.readTranscript({sessionId: `local_${OTHER}`, mode: 'full', ticket: 1}), 'session');
fails(() => api.readTranscript({sessionId: follower.sessionId, mode: 'unexpected', ticket: 1}), 'page');
assert.equal(api.requestTranscriptPort(), false);
""")

    def test_all_mutations_and_unknown_controls_are_denied(self):
        self.run_js("""
for (const method of ['start','prewarm','prewarmFolderless','retryStart','sendMessage','sendSteeredNow',
  'stop','interrupt','clearSession','forkSession','rewindSession','resumeCliSession','importAllCliSessions',
  'setModel','setEffort','setPermissionMode','respondToToolPermission','applyFlagSettings',
  'startShellPty','writeShellPty','discardWorkingTree','futureUnreviewedMethod']) {
  fails(() => api[method](follower.sessionId, 'must not be delivered'), 'readonly');
}
assert.equal(api.warmSession(follower.sessionId), undefined);
assert.equal(api.personOpenedSession(follower.sessionId), undefined);
assert.equal(api.setVisibility(follower.sessionId, true), undefined);
assert.equal(api.canAutoResume(follower.sessionId), false);
fails(() => api.warmSession(`local_${OTHER}`), 'session');
assert.equal(follower.nextSequence, 0);
assert.deepEqual(events, []);
""")

    def test_untrusted_callers_cannot_mutate_stored_messages(self):
        self.run_js("""
follower.subscribe(e => { if (e.message) e.message.message.content[0].text = 'changed'; });
follower.announce();
const input = row('original', 'unchanged');
follower.ingest(batch(0, [input]));
input.message.content[0].text = 'changed externally';
const snapshot = api.getTranscript(follower.sessionId);
snapshot[0].message.content[0].text = 'changed snapshot';
assert.equal(api.getTranscript(follower.sessionId)[0].message.content[0].text, 'unchanged');
assert.equal(events[1].message.message.content[0].text, 'unchanged');
const off = follower.subscribe(() => { throw new Error('disposed listener called'); });
off();
follower.ingest(batch(1, [row('next')]));
""")

    def test_disconnected_renderer_cannot_drop_other_messages(self):
        self.run_js("""
follower.announce();
follower.subscribe(() => { throw new Error('renderer destroyed'); });
follower.ingest(batch(0, [row('first'), row('second')]));
assert.deepEqual(events.slice(1).map(e => e.message.uuid), ['first', 'second']);
assert.equal(api.getTranscript(follower.sessionId).length, 2);
assert.equal(follower.takeDeliveryErrors().length, 1);
follower.ingest(batch(2, [row('third')]));
assert.deepEqual(follower.takeDeliveryErrors(), []);
assert.equal(events.at(-1).message.uuid, 'third');
""")

    def test_listener_ack_tracks_actual_registration_and_disposal(self):
        self.run_js("""
const registrations = new Map();
const notices = [];
const t = {ipcRenderer: {
  on(channel, listener) { registrations.set(channel, listener); },
  removeListener(channel, listener) {
    assert.equal(registrations.get(channel), listener);
    registrations.delete(channel);
  },
  send(channel, notice) { notices.push({channel, notice, registered: registrations.size}); },
}};
const Y = 'native-prefix:';
for (const patch of makeListenerPatches('claudex-bridge:ready:fixture-nonce')) {
  const handler = eval('({' + patch.replacement + '})');
  const method = patch.kind === 'navigation' ? 'onNavigate' : 'onOnEvent';
  let received;
  const off = handler[method](value => { received = value; });
  const notice = notices.at(-1);
  assert.equal(notice.registered, 1);
  assert.equal(notice.notice.present, true);
  const registration = [...registrations.values()][0];
  registration({}, 'actual native payload');
  assert.equal(received, 'actual native payload');
  off();
  assert.equal(notices.at(-1).registered, 0);
  assert.equal(notices.at(-1).notice.present, false);
  assert.equal(notices.at(-1).notice.subscription, notice.notice.subscription);
}
fails(() => makeListenerPatches('bad"injection'), 'channel');
""")


if __name__ == "__main__":
    unittest.main()
