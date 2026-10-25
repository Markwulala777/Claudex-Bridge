// Native Claude Code 2.1.295 function-hook module. prepare_plugin replaces BINDING.
// No model API, subprocess, login, policy change, or synthetic assistant message.
const BINDING = __CLAUDEX_OWNER_BINDING__;

  const instance = BINDING.launch_id + "-" + Math.random().toString(36).slice(2);
  let sequence = 0;
  let writes = Promise.resolve();
  let initialized = false;
  let initializing;
  let active;
  let polling = false;
  let timer;
  const stopSeen = new Set();

  function emit($, kind, data) {
    const number = ++sequence;
    const base = BINDING.state_dir + "/events/" + instance + "-" + String(number).padStart(8, "0");
    const record = {
      schema: 1, kind, session_id: BINDING.session_id,
      claude_code_version: BINDING.claude_code_version,
      launch_id: BINDING.launch_id, instance_id: instance,
      sequence: number, ...data,
    };
    // A commit marker is written only after the complete JSON write succeeds.
    // The queue remains rejected after any IO failure: no later success is claimed.
    writes = writes.then(async () => {
      await $.fs.write(base + ".json", JSON.stringify(record));
      await $.fs.write(base + ".commit", "1");
    });
    return writes;
  }

  async function read($, path) {
    if (!await $.fs.exists(path)) return null;
    return JSON.parse(await $.fs.read(path));
  }

  async function identity($) {
    const session = await $.session.id();
    const version = await $.session.version();
    if (session !== BINDING.session_id || version.version !== BINDING.claude_code_version) {
      throw new Error("Claudex owner session/version changed");
    }
    return session;
  }

  async function pollStop($) {
    if (polling || !initialized) return;
    polling = true;
    try {
      await identity($);
      const command = await read($, BINDING.state_dir + "/interrupt.json");
      if (!command || typeof command.command_id !== "string" || stopSeen.has(command.command_id)) return;
      stopSeen.add(command.command_id);
      const exact = command.schema === 1 && command.session_id === BINDING.session_id &&
        command.launch_id === BINDING.launch_id && command.instance_id === instance &&
        active && command.request_id === active.request_id && command.turn_id === active.turn_id;
      if (!exact) {
        await emit($, "interrupt_rejected", {command_id: command.command_id, reason: "identity_or_turn_mismatch"});
        return;
      }
      try {
        await $.turn.abort({turnId: active.turn_id});
        await emit($, "interrupt_requested", {
          command_id: command.command_id, request_id: command.request_id, turn_id: command.turn_id,
        });
      } catch (error) {
        await emit($, "interrupt_rejected", {
          command_id: command.command_id, request_id: command.request_id, turn_id: command.turn_id,
          reason: String(error.message || error),
        });
      }
    } finally { polling = false; }
  }

  function initialize($) {
    if (!initializing) initializing = (async () => {
      const actual = await identity($);
      await emit($, "ready", {actual_session_id: actual});
      // This file is convenient for humans. Readers use the committed ready event.
      await $.fs.write(BINDING.state_dir + "/ready.json", JSON.stringify({
        schema: 1, session_id: actual, claude_code_version: BINDING.claude_code_version,
        launch_id: BINDING.launch_id, instance_id: instance,
      }));
      initialized = true;
      timer = $.clock.every(150, async () => { await pollStop($); });
    })();
    return initializing;
  }

export function register(on) {
  on("session.start", async ($, event, next) => {
    await initialize($);
    return next(event);
  });

  on("turn.start", async ($, event, next) => {
    if (event.agentId !== undefined) return next(event);
    await initialize($);
    await identity($);
    const request = await read($, BINDING.state_dir + "/active.json");
    const exact = request && request.schema === 1 && request.session_id === BINDING.session_id &&
      request.launch_id === BINDING.launch_id && request.instance_id === instance &&
      typeof request.request_id === "string" && typeof request.marker === "string" &&
      typeof event.turnId === "string" && typeof event.text === "string" &&
      event.text.startsWith(request.marker + "\n");
    if (exact && !active) {
      active = {request_id: request.request_id, turn_id: event.turnId};
      await emit($, "turn_start", active);
    } else {
      await emit($, "unclaimed_turn_start", {turn_id: event.turnId});
    }
    return next(event);
  });

  on("turn.complete", async ($, event, next) => {
    if (event.agentId !== undefined) return next(event);
    await initialize($);
    await identity($);
    if (active && event.turnId === active.turn_id) {
      const claimed = active;
      active = undefined;
      const valid = ["answer", "error", "refusal", "aborted"].includes(event.reason) &&
        typeof event.isAborted === "boolean" && (event.reason === "aborted") === event.isAborted &&
        typeof event.answer === "string";
      if (!valid) {
        await emit($, "protocol_error", {...claimed, reason: "invalid_native_turn_complete"});
      } else {
        await emit($, "turn_complete", {
          ...claimed, reason: event.reason, isAborted: event.isAborted,
          answer: event.answer, durationMs: event.durationMs,
          ...(event.refusal === undefined ? {} : {refusal: event.refusal}),
          ...(event.usage === undefined ? {} : {usage: event.usage}),
        });
      }
    }
    return next(event);
  });

  on("classic.PermissionRequest", async ($, event, next) => {
    if (event.agent_id !== undefined) return next(event);
    await initialize($);
    await identity($);
    if (active) await emit($, "permission_request", {
      ...active, prompt_id: event.prompt_id, tool_name: event.tool_name,
    });
    return next(event);
  });

  on("session.end", async ($, event, next) => {
    if (timer) timer.cancel();
    if (initialized) await emit($, "session_end", {...(active || {}), reason: event.reason});
    return next(event);
  });
}
