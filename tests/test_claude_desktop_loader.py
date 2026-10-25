"""Run the real loader in Node VM with no Electron, IPC, or child processes."""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "skills/claudex-bridge/scripts/claude_desktop_loader.js"
CORE = ROOT / "skills/claudex-bridge/scripts/claude_desktop_follower.js"
NODE = shutil.which("node")


HARNESS = r"""
const assert = require('node:assert/strict');
const realFs = require('node:fs');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const core = require(CORE_PATH);
const code = realFs.readFileSync(LOADER_PATH, 'utf8');
const UUID = '11111111-1111-4111-8111-111111111111';
const OTHER = '22222222-2222-4222-8222-222222222222';
const LOCAL = `local_${UUID}`;
const CHANNEL = 'claudex-bridge:ready:offline-fixture';
const fixtureBinding = {
  schema: 1, session_id: UUID, desktop_version: '3.6556.1',
  claude_code_version: '2.1.295', cwd: '/tmp/claudex-loader-fixture',
  title: 'Offline fixture',
};
const row = (uuid, text = 'actual recorded text') => ({
  type: 'assistant', session_id: UUID, uuid,
  message: {role: 'assistant', content: [{type: 'text', text}]},
});
const batch = (kind, sequence, messages, extra = {}) => ({
  kind, session_id: UUID, source_identity: 'fixture-device:fixture-inode',
  claude_code_version: '2.1.295', sequence, messages, ...extra,
});
const frameBytes = frame => Buffer.from(JSON.stringify(frame) + '\n');
const tick = () => new Promise(resolve => setImmediate(resolve));

class WebContents extends EventEmitter {
  constructor() { super(); this.mainFrame = {}; this.destroyed = false; }
  isDestroyed() { return this.destroyed; }
  destroy() { this.destroyed = true; this.emit('destroyed'); }
}

function harness(overrides = {}) {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kills = 0;
  child.kill = () => { child.kills += 1; return true; };
  const ipc = new EventEmitter();
  const wc = new WebContents();
  const events = [], navigations = [], spawns = [], reads = [];
  const timers = new Map();
  let timerId = 0;
  const bindingPath = '/fixture/binding.json';
  const options = {
    bindingPath, python: '/fixture/python3', sourceScript: '/fixture/source.py',
    webContents: wc, onEvent: event => events.push(event),
    navigate: route => navigations.push(route), ...overrides,
  };
  const fakeRequire = name => {
    if (name === 'node:fs') return {
      readFileSync(file, encoding) {
        reads.push(file);
        assert.equal(file, bindingPath);
        assert.equal(encoding, 'utf8');
        return JSON.stringify(fixtureBinding);
      },
    };
    if (name === 'node:child_process') return {
      spawn(binary, args, options) {
        spawns.push({binary, args: Array.from(args), options});
        return child;
      },
    };
    if (name === './claude_desktop_follower.js') return core;
    throw new Error(`Unexpected dependency: ${name}`);
  };
  const module = {exports: {}};
  const context = {
    require: fakeRequire, module, exports: module.exports, Buffer,
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(code, context, {filename: 'claude_desktop_loader.js'});
  const loader = module.exports;
  loader.installListenerGate(ipc, CHANNEL);
  let outcome = {state: 'not-started'};
  const h = {
    child, ipc, wc, events, navigations, spawns, reads, timers,
    get outcome() { return outcome; },
    get api() {
      assert.equal(outcome.state, 'fulfilled', 'loader must initialize before API use');
      return outcome.value;
    },
    begin() {
      assert.equal(outcome.state, 'not-started');
      outcome = {state: 'pending'};
      // Attach both handlers immediately; tests may intentionally reject before await.
      loader.create(options).then(
        value => { outcome = {state: 'fulfilled', value}; },
        error => { outcome = {state: 'rejected', error}; },
      );
    },
    data(bytes) { child.stdout.emit('data', bytes); },
    feed(frame) { this.data(frameBytes(frame)); },
    ack(kind, present = true, subscription = `${kind}-subscription`, sender = wc,
        senderFrame = sender?.mainFrame, channel = CHANNEL) {
      ipc.emit(channel, {sender, senderFrame}, {kind, present, subscription});
    },
    expireTimers() {
      for (const [id, timer] of [...timers]) {
        timers.delete(id);
        timer.fn();
      }
    },
    async start(messages = [row('history')]) {
      this.begin();
      this.feed(batch('snapshot', 0, messages));
      await tick();
      assert.equal(outcome.state, 'fulfilled', outcome.error?.stack);
      return this.api;
    },
  };
  return h;
}

function assertUnavailable(h) {
  assert.equal(h.api.getSession(LOCAL).transcriptUnavailable, true);
  assert.equal(h.api.getSession(LOCAL).turnRunning, undefined);
  assert.equal(h.child.kills, 1, 'failed source must be cleaned up exactly once');
  assert.equal(h.events.some(event => event.type === 'error'), true);
  assert.equal(h.events.some(event => event.message?.type === 'result'), false,
    'transport failure must never manufacture completion');
}
"""


@unittest.skipUnless(NODE, "Node.js is required to test the Electron-side loader")
class NativeDesktopLoaderTests(unittest.TestCase):
    def run_js(self, body):
        script = (HARNESS.replace("LOADER_PATH", json.dumps(str(LOADER)))
                  .replace("CORE_PATH", json.dumps(str(CORE))))
        script += "\n(async () => {\n" + body + "\n})().catch(error => {\n"
        script += "console.error(error.stack); process.exitCode = 1;\n});\n"
        result = subprocess.run([NODE, "-e", script], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ack_after_creation_gates_navigation_and_event_delivery_independently(self):
        self.run_js(r"""
const h = harness();
await h.start();
assert.equal(h.api.getAll().length, 1);
assert.equal(h.api.getSessionList().sessions[0].sessionId, LOCAL);
assert.equal(h.api.getTranscript(LOCAL)[0].uuid, 'history');
assert.deepEqual(h.navigations, []);
assert.deepEqual(h.events, [], 'reading history does not establish a native listener');
h.ack('navigation');
assert.deepEqual(h.navigations, [`/epitaxy/${LOCAL}`]);
assert.deepEqual(h.events, [], 'navigation listener is not an event listener');
h.ack('navigation');
assert.equal(h.navigations.length, 1);
h.ack('events');
assert.deepEqual(h.events.map(e => e.type), ['start']);
h.ack('events');
assert.equal(h.events.length, 1, 'duplicate registration must not announce twice');
h.feed(batch('append', 1, [row('new')]));
assert.deepEqual(h.events.at(-1).message, row('new'));
assert.equal(h.api.getTranscript(LOCAL).length, 2);
assert.equal(h.timers.size, 0);
assert.equal(h.spawns.length, 1);
assert.equal(h.spawns[0].binary, '/fixture/python3');
assert.deepEqual(h.spawns[0].args, ['/fixture/source.py', '--binding', '/fixture/binding.json']);
assert.equal(h.spawns[0].options.shell, false);
assert.deepEqual(Array.from(h.spawns[0].options.stdio), ['ignore', 'pipe', 'pipe']);
assert.deepEqual(h.reads, ['/fixture/binding.json']);
""")

    def test_ack_before_snapshot_is_retained_but_cannot_publish_unverified_source(self):
        self.run_js(r"""
const h = harness();
h.ack('navigation');
h.ack('events');
h.begin();
await tick();
assert.deepEqual(h.navigations, []);
assert.deepEqual(h.events, []);
h.feed(batch('snapshot', 0, [row('history')]));
await tick();
assert.equal(h.outcome.state, 'fulfilled');
assert.deepEqual(h.navigations, [`/epitaxy/${LOCAL}`]);
assert.deepEqual(h.events.map(e => e.type), ['start']);
assert.equal(h.events[0].session.sessionId, LOCAL);
assert.equal(h.events[0].session.turnRunning, undefined);
""")

    def test_wrong_sender_frame_channel_and_invalid_notices_cannot_release_target(self):
        self.run_js(r"""
const h = harness();
await h.start();
const otherWindow = new WebContents();
const destroyedWindow = new WebContents();
destroyedWindow.destroy();
for (const kind of ['navigation', 'events']) {
  h.ack(kind, true, 'foreign-window', otherWindow);
  h.ack(kind, true, 'dead-window', destroyedWindow);
  h.ack(kind, true, 'child-frame', h.wc, {});
  h.ack(kind, true, 'wrong-channel', h.wc, h.wc.mainFrame, 'wrong-private-channel');
  h.ack(kind, true, 'null-sender', null, null);
  h.ack(kind, 'true');
  h.ack(kind, true, 42);
  h.ack(kind, true, 'x'.repeat(121));
}
h.ack('unexpected-kind');
assert.deepEqual(h.navigations, []);
assert.deepEqual(h.events, []);
h.ack('events');
assert.deepEqual(h.events.map(e => e.type), ['start']);
assert.deepEqual(h.navigations, []);
h.ack('navigation');
assert.deepEqual(h.navigations, [`/epitaxy/${LOCAL}`]);
""")

    def test_subscription_ids_and_navigation_lifetime_control_live_delivery(self):
        self.run_js(r"""
const h = harness();
await h.start();
h.ack('events', true, 'first');
h.ack('events', true, 'second');
h.ack('events', false, 'unknown');
h.ack('events', false, 'first');
h.feed(batch('append', 1, [row('still-subscribed')]));
assert.equal(h.events.at(-1).message.uuid, 'still-subscribed');
h.ack('events', false, 'second');
const count = h.events.length;
h.feed(batch('append', 2, [row('history-only')]));
assert.equal(h.events.length, count);
assert.equal(h.api.getTranscript(LOCAL).at(-1).uuid, 'history-only');
h.ack('events', true, 'third');
assert.equal(h.events.at(-1).type, 'start');
h.wc.emit('did-start-navigation', {}, '/same-document', true, true);
h.feed(batch('append', 3, [row('same-document')]));
assert.equal(h.events.at(-1).message.uuid, 'same-document');
h.wc.emit('did-start-navigation', {}, '/child-frame', false, false);
h.feed(batch('append', 4, [row('main-document-intact')]));
assert.equal(h.events.at(-1).message.uuid, 'main-document-intact');
h.wc.emit('did-start-navigation', {}, '/new-document', false, true);
const beforeNewDocumentAck = h.events.length;
h.feed(batch('append', 5, [row('new-document-history')]));
assert.equal(h.events.length, beforeNewDocumentAck);
h.ack('events', true, 'new-document-listener');
assert.equal(h.events.at(-1).type, 'start');
assert.equal(h.api.getTranscript(LOCAL).length, 6);
""")

    def test_fragmented_utf8_and_coalesced_records_preserve_exact_history(self):
        self.run_js(r"""
const h = harness();
h.begin();
const original = row('unicode', '中文原始内容');
const bytes = frameBytes(batch('snapshot', 0, [original]));
const split = bytes.indexOf(Buffer.from('中')) + 1;
h.data(bytes.subarray(0, split));
await tick();
assert.equal(h.outcome.state, 'pending');
h.data(Buffer.concat([bytes.subarray(split), frameBytes(batch('append', 1, [row('next')]))]));
await tick();
assert.equal(h.outcome.state, 'fulfilled');
assert.deepEqual(h.api.getTranscript(LOCAL), [original, row('next')]);
assert.deepEqual(h.events, []);
h.ack('events');
h.feed(batch('append', 2, [row('live')]));
assert.deepEqual(h.events.map(e => e.type), ['start', 'message']);
assert.equal(h.events.at(-1).message.uuid, 'live');
""")

    def test_source_errors_and_child_exit_mark_unavailable_without_success(self):
        self.run_js(r"""
for (const failure of ['source_error', 'exit', 'error']) {
  const h = harness();
  await h.start();
  h.ack('events');
  if (failure === 'source_error') h.feed({kind: 'source_error', error: 'Fixture source changed'});
  else h.child.emit(failure, failure === 'error' ? new Error('fixture reader failure') : 17);
  assertUnavailable(h);
  h.child.emit('exit', 17);
  h.feed(batch('append', 1, [row('must-not-appear')]));
  assert.equal(h.child.kills, 1);
  assert.deepEqual(h.api.getTranscript(LOCAL).map(m => m.uuid), ['history']);
}
""")

    def test_source_stdout_eof_is_unavailable_even_before_child_exit(self):
        self.run_js(r"""
for (const streamEvent of ['end', 'close']) {
  const h = harness();
  await h.start();
  h.ack('events');
  h.child.stdout.emit(streamEvent);
  assertUnavailable(h);
}
""")

    def test_malformed_source_error_cannot_escape_cleanup(self):
        self.run_js(r"""
for (const errorFrame of [{kind: 'source_error'}, {kind: 'source_error', error: ''}]) {
  const h = harness();
  await h.start();
  h.ack('events');
  assert.doesNotThrow(() => h.feed(errorFrame));
  assertUnavailable(h);
  assert.equal(h.events.filter(event => event.type === 'error').every(event =>
    typeof event.error === 'string' && event.error.length > 0), true);
}
""")

    def test_source_stdout_error_is_unavailable_without_an_unhandled_exception(self):
        self.run_js(r"""
const h = harness();
await h.start();
h.ack('events');
assert.doesNotThrow(() => h.child.stdout.emit('error', new Error('fixture pipe read failed')));
assertUnavailable(h);
""")

    def test_incomplete_final_record_cannot_become_a_successful_message(self):
        self.run_js(r"""
const h = harness();
await h.start();
h.ack('events');
const bytes = frameBytes(batch('append', 1, [row('incomplete')]));
h.data(bytes.subarray(0, bytes.length - 7));
assert.deepEqual(h.api.getTranscript(LOCAL).map(m => m.uuid), ['history']);
h.child.stdout.emit('end');
assertUnavailable(h);
assert.deepEqual(h.api.getTranscript(LOCAL).map(m => m.uuid), ['history']);
""")

    def test_invalid_initial_snapshot_rejects_instead_of_hanging(self):
        self.run_js(r"""
for (const extra of [
  {session_id: OTHER}, {claude_code_version: 'future-version'}, {sequence: 3},
  {messages: [{...row('foreign'), session_id: OTHER}]},
]) {
  const h = harness();
  h.ack('navigation');
  h.ack('events');
  h.begin();
  h.feed(batch('snapshot', 0, [row('history')], extra));
  await tick();
  assert.equal(h.outcome.state, 'rejected', 'invalid first snapshot must settle initialization');
  assert.equal(h.child.kills, 1);
  assert.equal(h.timers.size, 0);
  assert.deepEqual(h.navigations, []);
  assert.deepEqual(h.events, []);
}
""")

    def test_initial_reader_error_and_timeout_reject_and_clean_up(self):
        self.run_js(r"""
for (const failure of ['source_error', 'spawn_error', 'exit', 'stdout-end', 'stdout-close',
    'stdout-error', 'timeout', 'invalid-json', 'wrong-kind']) {
  const h = harness();
  h.begin();
  if (failure === 'source_error') h.feed({kind: 'source_error', error: 'Fixture read failed'});
  if (failure === 'spawn_error') h.child.emit('error', new Error('fixture spawn failure'));
  if (failure === 'exit') h.child.emit('exit', 3);
  if (failure === 'stdout-end') h.child.stdout.emit('end');
  if (failure === 'stdout-close') h.child.stdout.emit('close');
  if (failure === 'stdout-error') h.child.stdout.emit('error', new Error('fixture pipe read failed'));
  if (failure === 'timeout') h.expireTimers();
  if (failure === 'invalid-json') h.data(Buffer.from('{invalid}\n'));
  if (failure === 'wrong-kind') h.feed(batch('append', 0, [row('early')]));
  await tick();
  assert.equal(h.outcome.state, 'rejected', failure);
  assert.equal(h.child.kills, 1, failure);
  assert.equal(h.timers.size, 0, failure);
  assert.deepEqual(h.events, []);
  assert.deepEqual(h.navigations, []);
}
""")

    def test_foreign_identity_version_or_sequence_does_not_enter_live_history(self):
        self.run_js(r"""
for (const extra of [
  {session_id: OTHER}, {source_identity: 'replaced-source'},
  {claude_code_version: 'future-version'}, {sequence: 0}, {sequence: 5},
  {messages: [row('valid-prefix'), {...row('foreign'), session_id: OTHER}]},
]) {
  const h = harness();
  await h.start();
  h.ack('events');
  h.feed(batch('append', 1, [row('untrusted')], extra));
  assertUnavailable(h);
  assert.deepEqual(h.api.getTranscript(LOCAL).map(m => m.uuid), ['history']);
  assert.equal(h.events.some(e => e.type === 'message'), false);
}
""")

    def test_closing_initialized_view_stops_reader_and_late_delivery(self):
        self.run_js(r"""
const h = harness();
await h.start();
h.ack('events');
const count = h.events.length;
h.wc.destroy();
assert.equal(h.child.kills, 1);
h.child.emit('exit', null, 'SIGTERM');
h.feed(batch('append', 1, [row('after-close')]));
h.ack('navigation');
h.ack('events');
assert.equal(h.events.length, count);
assert.deepEqual(h.navigations, []);
assert.deepEqual(h.api.getTranscript(LOCAL).map(m => m.uuid), ['history']);
assert.equal(h.child.kills, 1);
assert.equal(h.timers.size, 0);
""")

    def test_closing_view_before_snapshot_settles_pending_initialization(self):
        self.run_js(r"""
const h = harness();
h.begin();
h.wc.destroy();
h.child.emit('exit', null, 'SIGTERM');
await tick();
assert.equal(h.outcome.state, 'rejected', 'closed window must not retain a pending create promise');
assert.equal(h.child.kills, 1);
assert.equal(h.timers.size, 0);
h.feed(batch('snapshot', 0, [row('late')]));
assert.deepEqual(h.events, []);
assert.deepEqual(h.navigations, []);
""")

    def test_disconnected_event_dispatcher_fails_closed_on_announce_or_append(self):
        self.run_js(r"""
for (const failingType of ['start', 'message']) {
  const delivered = [];
  const h = harness({onEvent(event) {
    if (event.type === failingType) throw new Error('fixture dispatcher disconnected');
    delivered.push(event);
  }});
  await h.start();
  h.ack('events');
  if (failingType === 'message') h.feed(batch('append', 1, [row('first'), row('second')]));
  assert.equal(h.api.getSession(LOCAL).transcriptUnavailable, true);
  assert.equal(h.child.kills, 1);
  assert.equal(h.api.getTranscript(LOCAL).length, failingType === 'message' ? 3 : 1);
  h.feed(batch('append', 3, [row('late')]));
  assert.equal(h.api.getTranscript(LOCAL).some(m => m.uuid === 'late'), false);
  assert.equal(delivered.some(event => event.message?.type === 'result'), false);
}
""")

    def test_disconnected_navigation_dispatcher_stops_reader(self):
        self.run_js(r"""
const h = harness({navigate() { throw new Error('fixture navigation dispatcher disconnected'); }});
await h.start();
h.ack('events');
h.ack('navigation');
assertUnavailable(h);
h.ack('navigation', false);
h.ack('navigation');
assert.equal(h.child.kills, 1);
""")


if __name__ == "__main__":
    unittest.main()
