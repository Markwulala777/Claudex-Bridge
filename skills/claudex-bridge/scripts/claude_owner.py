#!/usr/bin/env python3
"""Control one native Claude TUI through its verified 2.1.295 interfaces.

The native plugin is the authority for turn identity and completion. Socket EOF,
registry idle, timeout and successful sendall never mean task completion. This
module never starts a model, reads auth keys, retries a dispatched prompt, changes
providers, or overrides a feature gate. Linux only, Python standard library.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time
import uuid

VERSION = "2.1.295"
OWNER_ENV = {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
MAX_JSON = 4 * 1024 * 1024
MAX_MESSAGE = 900000
MAX_RECEIPT = 1024 * 1024


class OwnerError(RuntimeError):
    pass


def _uuid(value):
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise OwnerError("Expected an exact UUID") from exc
    if result != value:
        raise OwnerError("UUID must use canonical lowercase spelling")
    return result


def _json(path, limit=MAX_JSON, with_digest=False):
    path = Path(path)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise OwnerError("Untrusted or oversized state file: " + str(path))
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise OwnerError("State exceeds size limit")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise OwnerError("State must be a JSON object")
        return (value, hashlib.sha256(data).hexdigest()) if with_digest else value
    finally:
        os.close(fd)


def _write(path, value, exclusive=False):
    path = Path(path)
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _private(path):
    path = Path(path).absolute()
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OwnerError("State directory must be a real, user-owned private directory (0700)")
    return path


def prepare_plugin(private_dir, session_id):
    """Create a fresh private plugin and return paths/arguments; never start Claude."""
    session_id = _uuid(session_id)
    root = Path(private_dir).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = _private(root)
    if any(root.iterdir()):
        raise OwnerError("Use a fresh empty directory; existing state is never reset")
    binding = {"schema": 1, "session_id": session_id, "claude_code_version": VERSION,
               "state_dir": str(root), "launch_id": str(uuid.uuid4())}
    plugin = root / "plugin"
    for directory in (root / "events", root / "requests", root / "receipts",
                      plugin / ".claude-plugin", plugin / "hooks"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(root / "binding.json", binding, exclusive=True)
    _write(plugin / ".claude-plugin" / "plugin.json", {"name": "claudex-owner", "version": "0.1.0"})
    _write(plugin / "hooks" / "hooks.json", {"modules": ["./bridge.js"]})
    template = Path(__file__).with_name("claude_turn_hooks.js").read_text(encoding="utf-8")
    if template.count("__CLAUDEX_OWNER_BINDING__") != 1:
        raise OwnerError("Invalid native hook template")
    module = template.replace("__CLAUDEX_OWNER_BINDING__", json.dumps(binding, ensure_ascii=True))
    target = plugin / "hooks" / "bridge.js"
    target.write_text(module, encoding="utf-8")
    target.chmod(0o600)
    return {**binding, "plugin_dir": str(plugin), "ready_file": str(root / "ready.json"),
            "launch_args": ["--plugin-dir", str(plugin)], "launch_env": dict(OWNER_ENV)}


def _binding(private_dir):
    root = _private(private_dir)
    binding = _json(root / "binding.json")
    if (binding.get("schema") != 1 or binding.get("claude_code_version") != VERSION or
            binding.get("state_dir") != str(root)):
        raise OwnerError("Unsupported or relocated owner binding")
    _uuid(binding.get("session_id"))
    _uuid(binding.get("launch_id"))
    return root, binding


@contextmanager
def _lock(root, name=".lock"):
    fd = os.open(str(root / name), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise OwnerError("Another controller operation is active") from exc
    finally:
        os.close(fd)


def _start(pid, proc_root="/proc"):
    try:
        fields = (Path(proc_root) / str(pid) / "stat").read_text().rsplit(")", 1)[1].strip().split()
        if fields[0] in ("Z", "X") or not fields[19].isdigit():
            raise ValueError("not live")
        return fields[19]
    except (OSError, IndexError, ValueError) as exc:
        raise OwnerError("Owner process identity is unavailable") from exc


@dataclass(frozen=True)
class Owner:
    session_id: str
    pid: int
    proc_start: str
    socket_path: str
    socket_dev: int
    socket_ino: int
    registry_path: str
    uid: int
    status: str
    waiting_for: str = ""
    version: str = VERSION


def _owner(record, registry_path, proc_root):
    pid = record.get("pid")
    if (type(pid) is not int or pid <= 0 or record.get("version") != VERSION or
            record.get("kind") != "interactive" or record.get("peerProtocol") != 1 or
            "notify_idle" not in record.get("peerFeatures", [])):
        raise OwnerError("Owner is not a supported interactive Claude 2.1.295 peer")
    actual = _start(pid, proc_root)
    if record.get("procStart") != actual:
        raise OwnerError("Owner PID was recycled or registry is stale")
    path = Path(record.get("messagingSocketPath", ""))
    if not path.is_absolute() or len(os.fsencode(path)) > 103 or path.resolve() != path:
        raise OwnerError("Invalid native messaging socket path")
    info = path.lstat()
    parent = path.parent.stat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
        raise OwnerError("Native socket or its directory is not private to this user")
    return Owner(_uuid(record.get("sessionId")), pid, actual, str(path), info.st_dev,
                 info.st_ino, str(registry_path), info.st_uid,
                 str(record.get("status", "unknown")), str(record.get("waitingFor", "")))


def locate(session_id, registry_dir=None, proc_root="/proc"):
    """Find exactly one supported live TUI owner, without connecting to it."""
    session_id = _uuid(session_id)
    registry = Path(registry_dir) if registry_dir else Path.home() / ".claude/sessions"
    matches = []
    for path in registry.glob("*.json"):
        if not path.stem.isdigit():
            continue
        try:
            record = _json(path, 65536)
        except (OSError, ValueError, OwnerError):
            continue
        if record.get("sessionId") != session_id:
            continue
        matches.append(_owner(record, path, proc_root))
    if len(matches) != 1:
        raise OwnerError("Expected exactly one supported live owner for the exact session")
    return matches[0]


def _revalidate(owner, proc_root="/proc"):
    current = _owner(_json(owner.registry_path, 65536), owner.registry_path, proc_root)
    keys = ("session_id", "pid", "proc_start", "socket_path", "socket_dev", "socket_ino", "uid", "version")
    if any(getattr(current, key) != getattr(owner, key) for key in keys):
        raise OwnerError("Native owner identity changed")
    return current


def _check_project(owner, expected_project, proc_root="/proc"):
    """Bind dispatch to the native process's actual working directory."""
    if expected_project is None:
        return
    expected = Path(expected_project)
    if not expected.is_absolute() or not expected.is_dir():
        raise OwnerError("Expected project must be an existing absolute directory")
    try:
        actual = (Path(proc_root) / str(owner.pid) / "cwd").resolve(strict=True)
    except OSError as exc:
        raise OwnerError("Native owner working directory is unavailable") from exc
    _revalidate(owner, proc_root)
    if actual != expected.resolve():
        raise OwnerError("Native owner working directory does not match the task project")


def _check_execution_policy(owner, proc_root="/proc"):
    """Require native foreground cancellation, without changing configuration.

    In 2.1.295 turn.abort otherwise backgrounds Bash instead of stopping it.
    Read only the required flag from the *owner's* launch environment; never
    expose environment contents or infer it from the controller's environment.
    """
    try:
        with (Path(proc_root) / str(owner.pid) / "environ").open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
    except OSError as exc:
        raise OwnerError("Cannot verify the native owner's execution policy") from exc
    name = b"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="
    values = [part[len(name):] for part in raw.split(b"\0") if part.startswith(name)]
    if len(raw) > 1024 * 1024 or values != [b"1"]:
        raise OwnerError("Resume the idle native owner with CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1; "
                         "otherwise turn cancellation can leave commands running")
    _revalidate(owner, proc_root)


def _permission_update(pending, event):
    """Each native dialog invocation resolves independently, including batches."""
    key = event.get("permission_id")
    if event["kind"] == "permission_request":
        # Old logs remain readable; they did not record resolution events.
        key = key or "legacy-" + str(event["sequence"])
        if not isinstance(key, str) or key in pending:
            raise OwnerError("Duplicate or invalid native permission request")
        pending.add(key)
    else:
        if not isinstance(key, str) or key not in pending:
            raise OwnerError("Native permission resolution has no matching request")
        pending.remove(key)


def _peer(connection, owner):
    pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid != owner.pid or uid != owner.uid:
        raise OwnerError("Native socket peer PID/UID does not match the bound owner")


def _events(root, binding):
    events = []
    for committed in sorted((root / "events").glob("*.commit")):
        event_path = committed.with_suffix(".json")
        event, digest = _json(event_path, with_digest=True)
        if (event.get("schema") != 1 or event.get("session_id") != binding["session_id"] or
                event.get("launch_id") != binding["launch_id"] or
                event.get("claude_code_version") != VERSION):
            raise OwnerError("Native hook event identity/version mismatch")
        if type(event.get("sequence")) is not int or event["sequence"] < 1:
            raise OwnerError("Native event sequence is invalid")
        terminal = event.get("kind") == "turn_complete"
        evidence_status = ({"answer": "completed", "error": "failed", "refusal": "failed",
                            "aborted": "interrupted"}.get(event.get("reason")) if terminal else None)
        event["evidence"] = {"path": str(event_path), "sha256": digest,
                             "event_type": "claude.turn.complete" if terminal else event.get("kind"),
                             "session_id": binding["session_id"], "execution_id": event.get("turn_id"),
                             "id_kind": "turn", "status": evidence_status}
        events.append(event)
    groups = {}
    for event in events:
        groups.setdefault(event.get("instance_id"), []).append(event)
    for instance, group in groups.items():
        if not isinstance(instance, str) or [e["sequence"] for e in group] != list(range(1, len(group) + 1)):
            raise OwnerError("Native hook event sequence has a gap or duplicate")
    return events


def _ready(root, binding):
    ready = [e for e in _events(root, binding) if e.get("kind") == "ready"]
    if len(ready) != 1 or ready[0].get("actual_session_id") != binding["session_id"]:
        raise OwnerError("Expected one committed, exact-session native plugin ready event")
    return ready[0]


def _receipt(root, request, frame, owner):
    if (frame.get("type") != "control" or frame.get("action") != "peer_message_status" or
            frame.get("orig_msg_id") != request["msg_id"] or
            frame.get("from") != "uds:" + owner.socket_path or
            frame.get("status") not in ("held", "denied", "expired", "refused", "dropped", "delivered")):
        return
    _write(root / "receipts" / (uuid.uuid4().hex + ".json"), {
        "request_id": request["request_id"], "received_at": time.time(), "frame": frame,
    }, exclusive=True)


class _ReceiptInbox:
    def __init__(self, listener):
        self.listener = listener
        self.connections = {}

    def close(self):
        for connection in self.connections:
            connection.close()
        self.connections.clear()

    def poll(self, owner, root, request, timeout):
        import select
        readable = select.select([self.listener, *self.connections], [], [], timeout)[0]
        for connection in readable:
            if connection is self.listener:
                child, _ = self.listener.accept()
                try:
                    _peer(child, owner)
                    child.setblocking(False)
                    if len(self.connections) >= 16:
                        raise OwnerError("Too many native receipt connections")
                    self.connections[child] = b""
                except BaseException:
                    child.close()
                    raise
                continue
            try:
                block = connection.recv(65536)
                pending = self.connections[connection]
                if not block:
                    connection.close()
                    del self.connections[connection]
                    if pending:
                        raise OwnerError("Native receipt ended with an incomplete frame")
                    continue
                pending += block
                if len(pending) > MAX_RECEIPT:
                    raise OwnerError("Oversized native receipt")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    frame = json.loads(line)
                    if isinstance(frame, dict):
                        _receipt(root, request, frame, owner)
                self.connections[connection] = pending
            except BaseException:
                self.connections.pop(connection, None)
                connection.close()
                raise


def observe(private_dir, request_id=None):
    """Read committed facts only. Absence of completion yields pending/unknown."""
    root, binding = _binding(private_dir)
    ready = _ready(root, binding)
    if request_id is None:
        return {"status": "ready", **ready}
    request = _json(root / "requests" / (_uuid(request_id) + ".json"))
    if request.get("session_id") != binding["session_id"] or request.get("instance_id") != ready["instance_id"]:
        raise OwnerError("Request belongs to another owner instance")
    events = [e for e in _events(root, binding) if e.get("request_id") == request_id]
    starts = [e for e in events if e.get("kind") == "turn_start"]
    if len(starts) > 1:
        raise OwnerError("A request was associated with multiple native turns")
    turn_id = starts[0].get("turn_id") if starts else None
    if starts and (not isinstance(turn_id, str) or not turn_id):
        raise OwnerError("Native turn start has no exact execution ID")
    complete = [e for e in events if e.get("kind") == "turn_complete"]
    if len(complete) > 1 or (complete and (not turn_id or complete[0].get("turn_id") != turn_id)):
        raise OwnerError("Native completion does not match the claimed turn")
    receipts = [_json(p) for p in (root / "receipts").glob("*.json")]
    receipts = sorted((r for r in receipts if r.get("request_id") == request_id), key=lambda r: r["received_at"])
    status = "running" if turn_id else request.get("dispatch_status", "pending")
    if receipts and not turn_id:
        status = receipts[-1]["frame"]["status"]
    if any(e.get("kind") in ("protocol_error", "session_end") for e in events):
        status = "unavailable"
    pending_permissions = set()
    for event in events:
        if event.get("kind") in ("permission_request", "permission_resolved"):
            if event.get("turn_id") != turn_id:
                raise OwnerError("Permission event does not match the claimed turn")
            _permission_update(pending_permissions, event)
    if pending_permissions:
        status = "permission_wait"
    result = {"session_id": binding["session_id"], "request_id": request_id,
              "turn_id": turn_id, "status": status, "events": events, "receipts": receipts}
    if complete:
        end = complete[0]
        if (end.get("reason") not in ("answer", "error", "refusal", "aborted") or
                type(end.get("isAborted")) is not bool or not isinstance(end.get("answer"), str) or
                (end["reason"] == "aborted") != end["isAborted"]):
            raise OwnerError("Invalid native completion outcome")
        result.update(status="completed" if end["reason"] == "answer" and not end["isAborted"] else end["reason"], completion=end)
    return result


def view_state(private_dir, session_id):
    """Read the latest native lifecycle state for a viewer, without socket IO.

    Ready/registry-idle/EOF are not completion evidence. An unclaimed turn or
    unsupported event invalidates an earlier completion until a new exact claim
    establishes a lifecycle. A running claim additionally needs its original
    PID/start/socket identity to remain live. Invalid or unavailable evidence is
    reported as unknown; this function never creates files or starts an owner.
    """
    result = {"state": "unknown", "session_id": session_id, "turn_id": None}
    try:
        session_id = _uuid(session_id)
        root, binding = _binding(private_dir)
        if binding["session_id"] != session_id:
            raise OwnerError("Viewer session does not match the owner binding")
        events = _events(root, binding)
        ready = [event for event in events if event.get("kind") == "ready"]
        if (len(ready) != 1 or ready[0].get("actual_session_id") != session_id or
                ready[0].get("sequence") != 1 or
                any(event.get("instance_id") != ready[0]["instance_id"] for event in events)):
            raise OwnerError("Expected one exact-session native hook instance")
        result.update(instance_id=ready[0]["instance_id"], reason="ready_without_turn_evidence")
        active = None
        claimed_owner = None
        pending_permissions = set()

        def unknown(event, reason):
            return {"state": "unknown", "session_id": session_id,
                    "instance_id": ready[0]["instance_id"], "turn_id": event.get("turn_id"),
                    "reason": reason, "evidence": event.get("evidence")}

        for event in events[1:]:
            kind = event.get("kind")
            if kind == "turn_start":
                request_id = _uuid(event.get("request_id"))
                turn_id = event.get("turn_id")
                if not isinstance(turn_id, str) or not turn_id or active is not None:
                    raise OwnerError("Invalid or overlapping native claimed turns")
                request = _json(root / "requests" / (request_id + ".json"))
                if any(request.get(key) != expected for key, expected in (
                        ("schema", 1), ("session_id", session_id),
                        ("launch_id", binding["launch_id"]), ("instance_id", ready[0]["instance_id"]),
                        ("request_id", request_id))):
                    raise OwnerError("Native claim does not match its saved request")
                claimed_owner = Owner(**request["owner"])
                if claimed_owner.session_id != session_id or claimed_owner.version != VERSION:
                    raise OwnerError("Claimed native owner identity/version mismatch")
                active = (request_id, turn_id)
                pending_permissions.clear()
                result = {"state": "running", "session_id": session_id,
                          "instance_id": ready[0]["instance_id"], "request_id": request_id,
                          "turn_id": turn_id, "evidence": event["evidence"]}
            elif kind == "turn_complete":
                if (active is None or active != (event.get("request_id"), event.get("turn_id")) or
                        event.get("reason") not in ("answer", "error", "refusal", "aborted") or
                        type(event.get("isAborted")) is not bool or
                        (event["reason"] == "aborted") != event["isAborted"] or
                        not isinstance(event.get("answer"), str)):
                    result = unknown(event, "unmatched_or_invalid_native_completion")
                else:
                    reason = event["reason"]
                    result.update(state={"answer": "idle", "error": "failed", "refusal": "failed",
                                         "aborted": "stopped"}[reason], reason=reason,
                                  is_aborted=event["isAborted"], evidence=event["evidence"])
                    result.pop("waiting_for", None)
                active = None
            elif kind in ("permission_request", "permission_resolved"):
                if active != (event.get("request_id"), event.get("turn_id")):
                    result = unknown(event, "unmatched_native_permission_event")
                    active = None
                else:
                    _permission_update(pending_permissions, event)
                    result.update(evidence=event["evidence"])
                    if pending_permissions:
                        result["waiting_for"] = "permission"
                    else:
                        result.pop("waiting_for", None)
            elif kind == "session_end":
                result = {"state": "stopped", "session_id": session_id,
                          "instance_id": ready[0]["instance_id"], "turn_id": event.get("turn_id"),
                          "reason": "session_end", "native_reason": event.get("reason"),
                          "evidence": event["evidence"]}
                active = None
            elif kind in ("interrupt_requested", "interrupt_rejected"):
                # A rejected stale command does not change the native turn phase.
                if kind == "interrupt_requested" and active != (event.get("request_id"), event.get("turn_id")):
                    result = unknown(event, "unmatched_native_interrupt")
                    active = None
            else:
                result = unknown(event, kind or "unknown_native_event")
                active = None
        if result["state"] == "running":
            if claimed_owner is None:
                raise OwnerError("Running native turn has no saved owner identity")
            _revalidate(claimed_owner)
            result["owner_alive"] = True
        elif result.get("reason") in ("answer", "error", "refusal", "aborted") and (root / "active.json").exists():
            dispatched = _json(root / "active.json")
            if (dispatched.get("session_id") != session_id or
                    dispatched.get("instance_id") != ready[0]["instance_id"] or
                    dispatched.get("launch_id") != binding["launch_id"]):
                raise OwnerError("Latest dispatch belongs to another native owner instance")
            if dispatched.get("request_id") != result.get("request_id"):
                return {"state": "unknown", "session_id": session_id, "turn_id": None,
                        "instance_id": ready[0]["instance_id"], "reason": "new_dispatch_without_turn_evidence"}
        return result
    except (OSError, ValueError, TypeError, KeyError, OwnerError) as exc:
        return {"state": "unknown", "session_id": session_id, "turn_id": None, "reason": str(exc)}


def submit(private_dir, text, *, owner=None, request_id=None, timeout=30.0,
           registry_dir=None, proc_root="/proc", socket_factory=socket.socket, transport=None,
           expected_project=None):
    """Send once and collect receipts while waiting; timeout never causes a resend.

    Keep this call alive to receive permission receipts. observe() can still read
    lifecycle events after it returns; late native receipts require a new owner
    controller design and are not fabricated from registry/EOF.
    """
    root, binding = _binding(private_dir)
    if not isinstance(text, str) or not text.strip():
        raise OwnerError("Task text must be nonempty")
    if timeout < 0 or timeout > 86400:
        raise OwnerError("Invalid wait timeout")
    request_id = _uuid(request_id or str(uuid.uuid4()))
    with _lock(root):
        ready = _ready(root, binding)
        if (root / "active.json").exists():
            previous = _json(root / "active.json")
            previous_state = observe(root, previous["request_id"])
            if "completion" not in previous_state:
                raise OwnerError("Previous request has no native terminal event; do not resend or overlap")
        path = root / "requests" / (request_id + ".json")
        if path.exists():
            raise OwnerError("Request ID was already used; observe its state, do not resend")
        owner = owner or locate(binding["session_id"], registry_dir, proc_root)
        owner = _revalidate(owner, proc_root)
        if owner.session_id != binding["session_id"] or owner.status != "idle":
            raise OwnerError("Exact native owner must be idle before dispatch")
        _check_project(owner, expected_project, proc_root)
        _check_execution_policy(owner, proc_root)
        marker = "[claudex-request:" + request_id + "]"
        request = {"schema": 1, "session_id": binding["session_id"], "launch_id": binding["launch_id"],
                   "instance_id": ready["instance_id"], "request_id": request_id,
                   "msg_id": str(uuid.uuid4()), "message_uuid": str(uuid.uuid4()),
                   "marker": marker, "text": text, "dispatch_status": "prepared", "owner": asdict(owner)}
        _write(path, request, exclusive=True)
        _write(root / "active.json", request)
        reply_path = (transport.reply_path if transport else
                      Path(owner.socket_path).parent / ("cb-" + uuid.uuid4().hex[:20] + ".sock"))
        listener = transport.listener if transport else socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        inbox = transport.inbox if transport else _ReceiptInbox(listener)
        try:
            if not transport:
                listener.bind(str(reply_path))
                os.chmod(reply_path, 0o600)
                listener.listen(4)
            payload = {"type": "user", "session_id": owner.session_id, "uuid": request["message_uuid"],
                       "msg_id": request["msg_id"], "from": "uds:" + str(reply_path), "priority": "next",
                       "message": {"role": "user", "content": marker + "\n" + text}}
            wire = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(wire) > MAX_MESSAGE:
                raise OwnerError("Task exceeds native inbox size limit")
            with socket_factory(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(owner.socket_path)
                _peer(connection, owner)
                _revalidate(owner, proc_root)
                _check_project(owner, expected_project, proc_root)
                _check_execution_policy(owner, proc_root)
                # Persist ambiguity before sendall: any failure may be partial delivery.
                request["dispatch_status"] = "sending_unknown"
                _write(path, request)
                connection.sendall(wire)
                request["dispatch_status"] = "sent_unconfirmed"
                _write(path, request)
            deadline = time.monotonic() + timeout
            while True:
                state = observe(root, request_id)
                if "completion" in state or state["status"] in ("denied", "expired", "refused", "dropped", "unavailable"):
                    return state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {**state, "wait_timed_out": True}
                inbox.poll(owner, root, request, min(remaining, 0.2))
        finally:
            if not transport:
                inbox.close()
                listener.close()
                reply_path.unlink(missing_ok=True)


class OwnerController:
    """Keep the same Python process/reply socket alive across submit and observe.

    This context manager is the workflow integration API. Its submit returns
    immediately; observe drains late native permission receipts before reporting
    hook events. Exiting only closes our reply socket, never the Claude owner.
    """
    def __init__(self, private_dir, *, registry_dir=None, proc_root="/proc", socket_factory=socket.socket,
                 expected_project=None):
        self.root, self.binding = _binding(private_dir)
        self.registry_dir = registry_dir
        self.proc_root = proc_root
        self.socket_factory = socket_factory
        self.expected_project = expected_project
        self.owner = None
        self.listener = None
        self.reply_path = None
        self.inbox = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.inbox is not None:
            self.inbox.close()
            self.inbox = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.reply_path is not None:
            self.reply_path.unlink(missing_ok=True)
            self.reply_path = None

    def preflight(self):
        _ready(self.root, self.binding)
        owner = locate(self.binding["session_id"], self.registry_dir, self.proc_root)
        if self.owner is not None:
            _revalidate(self.owner, self.proc_root)
        if owner.status != "idle":
            raise OwnerError("Exact owner is not idle: " + owner.status)
        _check_project(owner, self.expected_project, self.proc_root)
        _check_execution_policy(owner, self.proc_root)
        self.owner = owner
        return asdict(owner)

    def _open(self):
        if self.listener is not None:
            return
        self.preflight()
        self.reply_path = Path(self.owner.socket_path).parent / ("cb-" + uuid.uuid4().hex[:20] + ".sock")
        self.listener = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.reply_path))
            os.chmod(self.reply_path, 0o600)
            self.listener.listen(4)
            self.inbox = _ReceiptInbox(self.listener)
        except BaseException:
            self.close()
            raise

    def submit(self, text, client_message_id):
        self._open()
        return submit(self.root, text, owner=self.owner, request_id=client_message_id, timeout=0,
                      registry_dir=self.registry_dir, proc_root=self.proc_root,
                      socket_factory=self.socket_factory, transport=self,
                      expected_project=self.expected_project)

    def observe(self, client_message_id, execution_id=None):
        request = _json(self.root / "requests" / (_uuid(client_message_id) + ".json"))
        if self.listener is not None:
            # Poll without sleeping; connection readers have a short bounded wait.
            for _ in range(32):
                self.inbox.poll(self.owner, self.root, request, 0)
        result = observe(self.root, client_message_id)
        if execution_id is not None and result["turn_id"] != execution_id:
            raise OwnerError("Observed native turn does not match the requested execution ID")
        if self.owner is not None and "completion" not in result:
            try:
                current = _revalidate(self.owner, self.proc_root)
                if current.status == "waiting" and result["turn_id"]:
                    result.update(status="permission_wait", waiting_for=current.waiting_for)
            except (OwnerError, OSError, ValueError) as exc:
                result.update(status="unavailable", owner_error=str(exc))
        return result

    def interrupt(self, client_message_id, execution_id):
        if self.owner is not None:
            _revalidate(self.owner, self.proc_root)
        return interrupt(self.root, client_message_id, execution_id)


def interrupt(private_dir, request_id, turn_id):
    """Ask the native hook to cancel only the exact known turn; no signals/keys."""
    root, binding = _binding(private_dir)
    with _lock(root, ".interrupt.lock"):
        state = observe(root, _uuid(request_id))
        if not isinstance(turn_id, str) or not turn_id or state["turn_id"] != turn_id or "completion" in state:
            raise OwnerError("Interrupt requires the exact still-running native turn")
        ready = _ready(root, binding)
        if (root / "interrupt.json").exists():
            previous = _json(root / "interrupt.json")
            answered = any(e.get("command_id") == previous.get("command_id") for e in _events(root, binding))
            if not answered:
                raise OwnerError("An interrupt request is still awaiting the native hook")
        command = {"schema": 1, "command_id": str(uuid.uuid4()), "session_id": binding["session_id"],
                   "launch_id": binding["launch_id"], "instance_id": ready["instance_id"],
                   "request_id": request_id, "turn_id": turn_id}
        _write(root / "interrupt.json", command)
        return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--state-dir", required=True)
    prepare.add_argument("--session-id", required=True)
    find = sub.add_parser("locate")
    find.add_argument("--session-id", required=True)
    for name in ("submit", "observe", "interrupt"):
        part = sub.add_parser(name)
        part.add_argument("--state-dir", required=True)
        part.add_argument("--request-id", required=name == "interrupt")
        if name == "submit":
            part.add_argument("--prompt-file", required=True)
            part.add_argument("--timeout", type=float, default=30.0)
        if name == "interrupt":
            part.add_argument("--turn-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            value = prepare_plugin(args.state_dir, args.session_id)
        elif args.action == "locate":
            value = asdict(locate(args.session_id))
        elif args.action == "observe":
            value = observe(args.state_dir, args.request_id)
        elif args.action == "interrupt":
            value = interrupt(args.state_dir, args.request_id, args.turn_id)
        else:
            value = submit(args.state_dir, Path(args.prompt_file).read_text(), request_id=args.request_id, timeout=args.timeout)
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except (OwnerError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
