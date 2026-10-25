"use strict";

// Loaded only by a prepared, version-pinned copy of Desktop. No original
// LocalSessionManager is initialized by this branch. The only spawned program
// is our read-only Python transcript reader, never Claude or another model.
const fs = require("node:fs");
const { spawn } = require("node:child_process");
const { NativeDesktopFollower, createLocalSessionsApi } = require("./claude_desktop_follower.js");
const MAX_FRAME = 16 * 1024 * 1024;
const subscriptions = new Map();

function installListenerGate(ipcMain, channel) {
  ipcMain.on(channel, (event, value) => {
    const wc = event.sender;
    if (!wc || wc.isDestroyed() || event.senderFrame !== wc.mainFrame ||
        !value || !["navigation", "events"].includes(value.kind) ||
        typeof value.subscription !== "string" || value.subscription.length > 120 ||
        typeof value.present !== "boolean") return;
    let state = subscriptions.get(wc);
    if (!state) {
      state = { navigation: new Set(), events: new Set(), callbacks: [] };
      subscriptions.set(wc, state);
      wc.once("destroyed", () => subscriptions.delete(wc));
      wc.on("did-start-navigation", (_event, _url, inPlace, mainFrame) => {
        if (mainFrame && !inPlace) { state.navigation.clear(); state.events.clear(); }
      });
    }
    const wasPresent = state[value.kind].size > 0;
    if (value.present) state[value.kind].add(value.subscription);
    else state[value.kind].delete(value.subscription);
    if (!wasPresent && state[value.kind].size) {
      for (const { kind, callback } of state.callbacks) if (kind === value.kind) callback();
    }
  });
}

function whenSubscribed(wc, kind, callback) {
  let state = subscriptions.get(wc);
  if (!state) {
    state = { navigation: new Set(), events: new Set(), callbacks: [] };
    subscriptions.set(wc, state);
    wc.once("destroyed", () => subscriptions.delete(wc));
    wc.on("did-start-navigation", (_event, _url, inPlace, mainFrame) => {
      if (mainFrame && !inPlace) { state.navigation.clear(); state.events.clear(); }
    });
  }
  state.callbacks.push({ kind, callback });
  if (state[kind].size) callback();
}

async function create(options) {
  const binding = JSON.parse(fs.readFileSync(options.bindingPath, "utf8"));
  const child = spawn(options.python, [options.sourceScript, "--binding", options.bindingPath],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true, shell: false });
  let follower;
  let pending = Buffer.alloc(0);
  let initialResolve, initialReject;
  let initialSettled = false;
  let closed = false;
  const initial = new Promise((resolve, reject) => { initialResolve = resolve; initialReject = reject; });
  function fail(message) {
    if (closed) return;
    closed = true;
    message = typeof message === "string" && message.trim() ? message :
      "The transcript reader failed without a diagnostic; execution state is unknown";
    if (follower) follower.sourceUnavailable(message);
    if (!initialSettled) {
      initialSettled = true;
      initialReject(new Error(message));
    }
    child.kill();
  }
  function checkDelivery() {
    if (follower.takeDeliveryErrors().length) {
      fail("The native event channel disconnected; reopen this viewer to read the verified history");
    }
  }
  child.on("error", () => fail("Cannot start the read-only transcript reader"));
  child.on("exit", () => fail("The transcript reader disconnected; execution state is unknown"));
  const sourceClosed = () => fail(pending.length ?
    "The transcript reader closed with an incomplete frame" :
    "The transcript reader stream closed; execution state is unknown");
  child.stdout.on("end", sourceClosed);
  child.stdout.on("close", sourceClosed);
  child.stdout.on("error", () => fail("The transcript reader stream failed; execution state is unknown"));
  // Drain diagnostic stderr, but do not publish paths or unrelated host details.
  child.stderr.on("data", () => {});
  child.stdout.on("data", chunk => {
    if (closed) return;
    pending = Buffer.concat([pending, chunk]);
    try {
      let boundary;
      while ((boundary = pending.indexOf(10)) !== -1) {
        if (boundary > MAX_FRAME) throw new Error("Transcript frame exceeds the size limit");
        const frame = JSON.parse(pending.subarray(0, boundary).toString("utf8"));
        pending = pending.subarray(boundary + 1);
        if (frame.kind === "source_error") throw new Error(frame.error);
        if (!follower) {
          if (frame.kind !== "snapshot") throw new Error("Missing initial transcript snapshot");
          follower = new NativeDesktopFollower({
            schema: 1, session_id: binding.session_id,
            desktop_version: binding.desktop_version, claude_code_version: binding.claude_code_version,
            cwd: binding.cwd, title: binding.title, owner_state: "unknown",
            source_identity: frame.source_identity,
          });
          follower.ingest(frame);
          checkDelivery();
          follower.subscribe(event => {
            if (!options.webContents.isDestroyed() && subscriptions.get(options.webContents)?.events.size) options.onEvent(event);
          });
          initialSettled = true;
          initialResolve(follower);
        } else {
          if (frame.kind !== "append") throw new Error("Unexpected repeated snapshot");
          follower.ingest(frame);
          checkDelivery();
        }
        if (closed) return;
      }
      if (pending.length > MAX_FRAME) throw new Error("Transcript frame exceeds the size limit");
    } catch (error) { fail(error.message); }
  });
  const timeout = setTimeout(() => fail("Transcript source initialization timed out"), 10000);
  options.webContents.once("destroyed", () => {
    fail("The native viewer closed before attachment completed");
  });
  await initial.finally(() => clearTimeout(timeout));
  const api = createLocalSessionsApi(follower);
  // A renderer calling the list proves it reached the native API, but not that
  // event/navigation listeners or its transcript are ready. Do not manufacture a
  // ready receipt. Explicit listener acknowledgments can release these events.
  let navigated = false;
  whenSubscribed(options.webContents, "navigation", () => {
    if (closed || navigated) return;
    try {
      options.navigate(follower.route);
      navigated = true;
    } catch (_error) { fail("The native navigation channel disconnected"); }
  });
  whenSubscribed(options.webContents, "events", () => {
    if (!closed) { follower.announce(); checkDelivery(); }
  });
  return api;
}

module.exports = { create, installListenerGate };
