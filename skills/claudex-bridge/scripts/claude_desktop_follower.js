"use strict";

/*
 * Pure, read-only adapter for the *native* LocalSessions interface. This module
 * does not read files, import Electron, spawn Claude, install patches, or change
 * authentication. A separately reviewed loader must bind a verified source and
 * supply its ordered SDK messages. It is not an installed Desktop integration.
 *
 * Protocol evidence: @ant/desktop 3.6556.1, app.asar/.vite/build/:
 * - CTo_fOxG: eb (native session view validator), LFt/VFt (transcript RPC);
 *   Jbt = full/page/tail, Ybt = inline/port; IN(id) = /epitaxy/<id>.
 * - CbirgK7f: sessionShown -> start; GG/KG -> message; session announcer ->
 *   session_updated. SDK stream_event is emitted but not added to history.
 * - DqktEifE: createLocalSessionsApi; dispatcher.dispatchOnEvent(event).
 *
 * Unknown controls fail closed. In particular warmSession is a viewer no-op:
 * the real method would spawn an SDK process even without sending a prompt.
 */

const VERSION = Object.freeze({ desktop: "3.6556.1", producer: "2.1.295" });
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const OWNER_STATES = new Set(["unknown", "running", "idle", "failed", "stopped"]);
const MESSAGE_TYPES = new Set([
  "user", "assistant", "system", "result", "stream_event", "tool_progress",
  "tool_use_summary", "auth_status", "rate_limit_event",
]);
const clone = value => JSON.parse(JSON.stringify(value));

class FollowerError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "FollowerError";
    this.code = code;
  }
}

function requireThat(condition, code, message) {
  if (!condition) throw new FollowerError(code, message);
}

function validateBinding(binding) {
  requireThat(binding && binding.schema === 1, "binding", "Unsupported binding schema");
  requireThat(binding.desktop_version === VERSION.desktop &&
    binding.claude_code_version === VERSION.producer, "version", "Unverified client or producer version");
  requireThat(typeof binding.session_id === "string" && UUID.test(binding.session_id),
    "binding", "An exact Claude session UUID is required");
  requireThat(typeof binding.cwd === "string" && binding.cwd.startsWith("/") &&
    !binding.cwd.includes("\0"), "binding", "An absolute source working directory is required");
  requireThat(typeof binding.source_identity === "string" && binding.source_identity.length > 0,
    "binding", "A verified source identity is required");
  requireThat(binding.owner_state === undefined || OWNER_STATES.has(binding.owner_state),
    "binding", "Invalid owner state");
  for (const key of ["title", "model", "effort", "permission_mode"]) {
    requireThat(binding[key] === undefined || typeof binding[key] === "string",
      "binding", `Invalid ${key} metadata`);
  }
  return clone({ ...binding, owner_state: binding.owner_state ?? "unknown" });
}

class NativeDesktopFollower {
  constructor(binding) {
    this.binding = Object.freeze(validateBinding(binding));
    this.sessionId = `local_${binding.session_id}`;
    this.nextSequence = 0;
    this.messages = [];
    this.listeners = new Set();
    this.deliveryErrors = [];
    this.announced = false;
    this.ownerState = this.binding.owner_state;
    this.unavailable = false;
  }

  get route() { return `/epitaxy/${encodeURIComponent(this.sessionId)}`; }

  requireSession(id) {
    requireThat(id === this.sessionId, "session", "This session is not bound to the follower");
  }

  view() {
    const b = this.binding;
    return {
      sessionId: this.sessionId, cwd: b.cwd, originCwd: b.cwd,
      // eb() requires a boolean isRunning; no native three-state field exists.
      // For unknown, this is only a disabled busy indicator, not an owner-idle
      // assertion. The authoritative state remains ownerState === 'unknown'.
      isRunning: this.ownerState === "running",
      footerIndicator: `Read-only follower: owner ${this.ownerState === "unknown" ? "execution state unknown" : this.ownerState}`,
      ...(this.ownerState === "unknown" ? {} : { turnRunning: this.ownerState === "running" }),
      isArchived: false, cliRowsFromElsewhere: true, processCliVersion: b.claude_code_version,
      ...(this.unavailable ? { transcriptUnavailable: true } : {}),
      ...(b.title === undefined ? {} : { title: b.title }),
      ...(b.model === undefined ? {} : { model: b.model }),
      ...(b.effort === undefined ? {} : { effort: b.effort }),
      ...(b.permission_mode === undefined ? {} : { permissionMode: b.permission_mode }),
    };
  }

  subscribe(listener) {
    requireThat(typeof listener === "function", "listener", "An event listener is required");
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(event) {
    // A renderer must not mutate the history or another renderer's event.
    for (const listener of this.listeners) {
      try { listener(clone(event)); }
      catch (error) {
        // A destroyed renderer must not lose the remainder of an admitted batch
        // or prevent other subscribers from receiving it. The loader must drain
        // these errors and treat that renderer as disconnected, not ready.
        this.listeners.delete(listener);
        this.deliveryErrors.push(error);
      }
    }
  }

  takeDeliveryErrors() {
    return this.deliveryErrors.splice(0);
  }

  announce() {
    this.announced = true;
    this.emit({ type: "start", sessionId: this.sessionId, session: this.view() });
  }

  ingest(batch) {
    requireThat(!this.unavailable, "source", "Source was interrupted; verify it again and create a new follower");
    requireThat(batch && batch.session_id === this.binding.session_id,
      "session", "Source session identity changed");
    requireThat(batch.source_identity === this.binding.source_identity,
      "source", "Source identity changed; a new binding is required");
    requireThat(batch.claude_code_version === this.binding.claude_code_version,
      "version", "Source version changed");
    requireThat(Number.isSafeInteger(batch.sequence) && batch.sequence === this.nextSequence,
      "sequence", "Source messages overlap or have a gap");
    requireThat(Array.isArray(batch.messages), "message", "Expected an ordered SDK message batch");
    requireThat(batch.owner_state === undefined || OWNER_STATES.has(batch.owner_state),
      "state", "Owner state must be observed, never inferred from EOF");
    // Validate the entire batch before advancing the cursor or emitting anything.
    const messages = batch.messages.map(message => {
      requireThat(message && message.session_id === this.binding.session_id,
        "session", "A message belongs to another session");
      requireThat(MESSAGE_TYPES.has(message.type), "message", "Unsupported SDK message type");
      if (message.type === "user" || message.type === "assistant") {
        requireThat(typeof message.uuid === "string" && message.uuid.length > 0 &&
          message.message && message.message.role === message.type,
        "message", "Malformed conversation message");
      }
      if (message.type === "stream_event") {
        requireThat(message.event && typeof message.event.type === "string",
          "message", "Malformed stream event");
      }
      return clone(message);
    });
    this.nextSequence += messages.length;
    for (const message of messages) {
      if (message.type !== "stream_event") this.messages.push(message);
      if (this.announced) this.emit({ type: "message", sessionId: this.sessionId, message });
    }
    // A static binding is not evidence of activity. Loaders following only a
    // transcript must use 'unknown'; other states require an owner observation.
    // Even an error result is passed through unchanged. Completion, failure, and
    // liveness come from the owner adapter, not guessed from message text/EOF.
    if (batch.owner_state !== undefined && batch.owner_state !== this.ownerState) {
      this.ownerState = batch.owner_state;
      if (this.announced) this.emit({ type: "session_updated", sessionId: this.sessionId, session: this.view() });
    }
  }

  sourceUnavailable(message) {
    requireThat(typeof message === "string" && message.length > 0, "source", "An error is required");
    this.unavailable = true;
    this.emit({ type: "error", sessionId: this.sessionId, error: message });
    this.emit({ type: "session_updated", sessionId: this.sessionId, session: this.view() });
  }

  transcript(id) {
    this.requireSession(id);
    return clone(this.messages);
  }

  page(id, beforeUuid = "", limit = 1000) {
    this.requireSession(id);
    requireThat(typeof beforeUuid === "string" && Number.isFinite(limit), "page", "Invalid page request");
    const count = Math.max(1, Math.min(Math.trunc(limit), 1000));
    const end = beforeUuid === "" ? this.messages.length : this.messages.findIndex(m => m.uuid === beforeUuid);
    if (end < 0) return { messages: [], hasMore: false, found: false, totalRows: this.messages.length };
    const start = Math.max(0, end - count);
    return { messages: clone(this.messages.slice(start, end)), hasMore: start > 0,
      found: true, totalRows: this.messages.length };
  }

  tail(id, limit = 500) {
    const { messages, hasMore } = this.page(id, "", Math.min(limit, 500));
    return { messages, hasMore };
  }

  readTranscript(request) {
    requireThat(request && Number.isFinite(request.ticket), "page", "A native transcript ticket is required");
    this.requireSession(request.sessionId);
    const base = { via: "inline", ticket: request.ticket };
    switch (request.mode) {
      case "full": return { ...base, messages: this.transcript(request.sessionId) };
      case "tail": return { ...base, ...this.tail(request.sessionId, request.limit ?? 500) };
      case "page": return { ...base, ...this.page(request.sessionId, request.beforeUuid ?? "", request.limit ?? 1000) };
      default: throw new FollowerError("page", "Unsupported native transcript mode");
    }
  }
}

function createLocalSessionsApi(follower) {
  requireThat(follower instanceof NativeDesktopFollower, "binding", "A bound follower is required");
  const requireSession = id => follower.requireSession(id);
  const implemented = {
    getAll: () => [follower.view()],
    getSessionList: () => ({ sessions: [follower.view()], loadGeneration: 1, archivedPending: false }),
    getSession: id => id === follower.sessionId ? follower.view() : null,
    getTranscript: id => follower.transcript(id),
    getTranscriptTail: (id, limit) => follower.tail(id, limit),
    getTranscriptPage: (id, before, limit) => follower.page(id, before, limit),
    readTranscript: request => follower.readTranscript(request),
    requestTranscriptPort: () => false, // Native code supports the inline fallback.
    loadArchivedSessions: () => undefined,
    warmSession: requireSession,
    personOpenedSession: requireSession,
    setVisibility: requireSession,
    setFocusedSession: id => { if (id !== null && id !== undefined) requireSession(id); },
    canAutoResume: id => { requireSession(id); return false; },
  };
  // No original manager/API is retained: unknown methods cannot fall through to
  // start, prewarm, send, interrupt, clear, fork, permission, or workspace writes.
  return new Proxy(implemented, {
    get(target, key) {
      if (typeof key !== "string" || key === "then") return undefined;
      if (Object.prototype.hasOwnProperty.call(target, key)) return target[key];
      return () => { throw new FollowerError("readonly", `LocalSessions.${key} is unavailable in the read-only follower`); };
    },
  });
}

const PRELOAD_ANCHORS = Object.freeze({
  navigation: 'onNavigate(e){let n=(t,n)=>e(n),r=Y+"Navigation_$_navigate";return t.ipcRenderer.on(r,n),()=>{t.ipcRenderer.removeListener(r,n)}}',
  events: 'onOnEvent(e){let n=(t,n)=>e(n),r=Y+"LocalSessions_$_onEvent";return t.ipcRenderer.on(r,n),()=>{t.ipcRenderer.removeListener(r,n)}}',
});

function makeListenerPatches(channel) {
  requireThat(typeof channel === "string" && /^[a-z0-9:_-]{16,160}$/.test(channel),
    "channel", "Use a private binding-specific IPC channel");
  return Object.entries(PRELOAD_ANCHORS).map(([kind, anchor]) => {
    const method = kind === "navigation" ? "onNavigate" : "onOnEvent";
    const nativeChannel = kind === "navigation" ? "Navigation_$_navigate" : "LocalSessions_$_onEvent";
    const notify = present => `t.ipcRenderer.send(${JSON.stringify(channel)},{kind:${JSON.stringify(kind)},subscription:i,present:${present}})`;
    return {
      kind, anchor,
      replacement: `${method}(e){let n=(t,n)=>e(n),r=Y+${JSON.stringify(nativeChannel)},i=Date.now().toString(36)+"-"+Math.random().toString(36).slice(2);return t.ipcRenderer.on(r,n),${notify(true)},()=>{t.ipcRenderer.removeListener(r,n);${notify(false)}}}`,
    };
  });
}

// A recipe, not an installer. The loader still needs source validation/tailing,
// real native-UI verification, renderer lifetime disposal and a readiness ack.
// It must run only in an isolated app copy and keep normal UI/auth eligibility.
const PATCH_RECIPE = Object.freeze({
  desktopVersion: VERSION.desktop,
  release: "8e298a7fb62db44b6c902bdcbbc3bb1a74d147f0",
  file: ".vite/build/index.chunk-CTo_fOxG.js",
  sha256: "3301b0b9d340c5ca4f6e69c83edfa9f984e2258d9709414571a0c6b544c63c36",
  factoryAnchor: 'let r=CW((async()=>{let[{createLocalSessionsApi:t},n]=await Promise.all([Promise.resolve().then((()=>require("./index.chunk-DqktEifE.js"))),Sw()]);return t({webContents:e},n,(e=>Ab.getDispatcher(e)))}));',
  insertion: "Add an explicit binding-only branch before Promise.all/Sw(); return createLocalSessionsApi(follower) in that branch. Retain the original factory unchanged otherwise.",
  eventDispatch: "Ab.getDispatcher(webContents)?.dispatchOnEvent(event)",
  navigate: "xb.getDispatcher(webContents)?.dispatchNavigate(follower.route)",
  nativeRoute: "/epitaxy/local_<exact-cli-session-uuid>",
  preload: Object.freeze({
    file: ".vite/build/mainView.js",
    sha256: "f28f3650f4fe3eb123d45da864b02e79fa223f249e860e6c5bd196082c8a9cba",
    anchors: PRELOAD_ANCHORS,
    readiness: "Validate IPC sender/frame and binding-specific channel; track subscription IDs and remove on present:false. Reset on document/process destruction. Navigation ack permits navigation; events ack permits event delivery. Neither means history applied or UI painted.",
  }),
  forbiddenShortcuts: Object.freeze([
    "Do not call importCliSession/adoptCliSession or write a Desktop ownership record.",
    "Do not change 1985233154 or other entitlement gates.",
    "Do not replace the global Claude binary or processWrapper.",
    "Do not fabricate login, init, model messages, result, or completion.",
    "Do not use claude://resume: its handler imports/adopts the session.",
  ]),
});

module.exports = { NativeDesktopFollower, createLocalSessionsApi, makeListenerPatches, FollowerError, VERSION, PATCH_RECIPE };
