"""Narrow native-owner interface for the shared workflow; no CLI fallback."""
from pathlib import Path
import sys


class AdapterError(RuntimeError):
    pass


class ClaudeWorkflowOwner:
    def __init__(self, session_id, owner_state_dir):
        from claude_owner import OwnerController
        self.controller = OwnerController(owner_state_dir)
        self.session_id = session_id
        if self.controller.binding["session_id"] != session_id:
            raise AdapterError("Claude owner binding belongs to another executor")

    def __enter__(self):
        self.controller.__enter__()
        return self

    def __exit__(self, *args):
        return self.controller.__exit__(*args)

    def preflight(self):
        result = self.controller.preflight()
        if result.get("session_id") != self.session_id:
            raise AdapterError("Claude preflight returned another session")
        return result

    def submit(self, prompt, client_message_id):
        result = self.controller.submit(prompt, client_message_id)
        return result.get("turn_id")

    def observe(self, client_message_id, execution_id=None):
        raw = self.controller.observe(client_message_id, execution_id)
        if raw.get("session_id") != self.session_id or raw.get("request_id") != client_message_id:
            raise AdapterError("Claude observation identity changed")
        result = {"execution_id": raw.get("turn_id"), "id_kind": "turn", "state": "unknown"}
        if "completion" in raw:
            end = raw["completion"]
            if end.get("turn_id") != result["execution_id"]:
                raise AdapterError("Claude completion does not match the original turn")
            evidence = end.get("evidence")
            if not evidence:
                raise AdapterError("Native completion has no durable evidence")
            result.update(state="terminal", terminal_status=evidence["status"],
                          evidence=evidence, result=end["answer"])
        elif raw["status"] in ("held", "permission_wait"):
            result.update(state="waiting", detail=raw.get("waiting_for") or raw["status"])
        elif raw["status"] == "running":
            result["state"] = "running"
        else:
            # Refusal before turn.start is not a fabricated native completion.
            # Retain ambiguity for exact recovery rather than inventing a turn ID.
            result["detail"] = raw["status"]
        return result

    def interrupt(self, client_message_id, execution_id):
        return self.controller.interrupt(client_message_id, execution_id)


def factory(task, transport, attempt_directory):
    if sys.platform != "linux":
        raise AdapterError("Native execution is currently implemented only on Linux")
    product = task["executor"]["product"]
    session_id = task["executor"]["session_id"]
    kind = transport.get("kind")
    if product == "claude" and kind == "claude-native":
        key = "owner_state_dir"
        owner = ClaudeWorkflowOwner
    elif product == "codex" and kind == "codex-desktop":
        from codex_owner import CodexOwner
        key = "rollout_path"
        owner = CodexOwner
    else:
        raise AdapterError("Transport does not match the fixed executor product")
    if set(transport) != {"kind", key} or not isinstance(transport[key], str) or not Path(transport[key]).is_absolute():
        raise AdapterError("Use an exact absolute native owner binding; extra transport options are unsupported")
    if product == "codex":
        return owner(session_id, transport[key], attempt_directory)
    return owner(session_id, transport[key])
