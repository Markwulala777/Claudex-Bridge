"""Local Linux Codex desktop adapter. No model is run by the bootstrap server.

The follower IPC is an internal desktop protocol, not a public stability promise.
Fail closed on protocol changes; never fall back to `codex exec`.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import time
import uuid


VERSIONS = {
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-load-complete-history": 1,
    "thread-follower-interrupt-turn": 4,
    "thread-stream-state-changed": 11,
    "thread-stream-following-changed": 1,
    "thread-stream-following-status-requested": 1,
}
MAX_FRAME = 268435456


class BridgeError(RuntimeError):
    pass


def resources_dir():
    return Path(os.environ.get("CODEX_WORKER_DESKTOP_RESOURCES", "/opt/codex-desktop/resources")).expanduser().resolve()


def verify_installation():
    """Inspect installed code, without executing it, before any IPC mutation."""
    resources = resources_dir()
    archive = resources / "app.asar"
    with archive.open("rb") as stream:
        header = struct.unpack("<4I", stream.read(16))
        if not 0 < header[3] < 32 * 1024 * 1024:
            raise BridgeError("Unsupported desktop ASAR header")
        tree = json.loads(stream.read(header[3]))
        nodes = tree["files"][".vite"]["files"]["build"]["files"]
        versions = None
        for name, node in nodes.items():
            if not name.startswith("src-") or not name.endswith(".js") or "offset" not in node:
                continue
            stream.seek(8 + header[1] + int(node["offset"]))
            source = stream.read(node["size"]).decode("utf-8")
            if '"thread-stream-state-changed":' in source:
                versions = {key: int(value) for key, value in re.findall(r'"(thread-[^"]+)":(\d+)', source)}
                break
        if versions is None or any(versions.get(key) != value for key, value in VERSIONS.items()):
            raise BridgeError("Desktop IPC version changed or is unsupported; no task was submitted. Update this adapter first.")
    binary = resources / "codex"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise BridgeError("Desktop's bundled codex executable is unavailable")
    executable = (resources.parent / "ChatGPT").resolve()
    running = False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == os.getuid() and (entry / "exe").resolve(strict=True) == executable:
                running = True
                break
        except OSError:
            continue
    if not running:
        raise BridgeError("Codex desktop must already be running in this Linux session; no task was submitted.")
    if not shutil.which("xdg-open"):
        raise BridgeError("xdg-open is required to open the desktop conversation")
    return binary


def codex_home():
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()


def validate_rollout(path):
    path = Path(path).resolve()
    if not path.is_relative_to(codex_home() / "sessions"):
        raise BridgeError("Unexpected rollout location; expected the shared local CODEX_HOME/sessions directory")
    return path


def log_event(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    stream.flush()


def create_thread(binary, project, title, directory, on_created, model=None, effort=None):
    """Create and persist an appServer thread, then release it to the desktop.

    No turn/start is sent here. Persist the returned ID before path validation or
    subsequent calls so a failed bootstrap cannot lose the created conversation.
    on_created first receives (thread_id, None), then the validated rollout path.
    A requested model/effort must be echoed by thread/start; otherwise the
    persisted thread is left idle and no task is sent.
    """
    with (directory / "bootstrap.jsonl").open("a", encoding="utf-8") as log, \
            (directory / "bootstrap-stderr.log").open("ab") as errors:
        proc = subprocess.Popen(
            # Preserve installed feature defaults. The worker task explicitly
            # permits delegation; Desktop retains explicitRequestOnly mode.
            [str(binary), "app-server"],
            cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            start_new_session=True,
        )
        buffer = bytearray()

        def send(value):
            log_event(log, {"direction": "out", **value})
            proc.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
            proc.stdin.flush()

        def request(method, params):
            identifier = str(uuid.uuid4())
            send({"id": identifier, "method": method, "params": params})
            deadline = time.monotonic() + 45
            while True:
                while b"\n" in buffer:
                    raw, _, rest = buffer.partition(b"\n")
                    buffer[:] = rest
                    message = json.loads(raw)
                    log_event(log, {"direction": "in", **message})
                    if message.get("id") != identifier:
                        continue
                    if "error" in message:
                        raise BridgeError(f"Bootstrap {method}: {message['error']}")
                    return message["result"]
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                    raise BridgeError(f"Bootstrap {method} timed out; inspect bootstrap.jsonl, do not blindly retry start.")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise BridgeError(f"Bootstrap exited during {method}; inspect bootstrap-stderr.log")
                buffer.extend(chunk)
                if len(buffer) > MAX_FRAME:
                    raise BridgeError("Bootstrap response exceeded the frame limit")

        try:
            request("initialize", {
                "clientInfo": {"name": "claude_codex_worker", "title": "Claude Codex Worker", "version": "2.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            send({"method": "initialized"})
            params = {"cwd": project, "ephemeral": False, "historyMode": "legacy", "threadSource": "user"}
            if model:
                params["model"] = model
            if effort:
                params["config"] = {"model_reasoning_effort": effort}
            started = request("thread/start", params)
            thread = started["thread"]
            thread_id = str(uuid.UUID(thread["id"]))
            on_created(thread_id, None)
            rollout = validate_rollout(thread["path"])
            on_created(thread_id, str(rollout))
            actual = {"model": started.get("model"), "effort": started.get("reasoningEffort")}
            if (model and actual["model"] != model) or (effort and actual["effort"] != effort):
                raise BridgeError(f"thread/start did not confirm model/effort {model!r}/{effort!r} (got {actual}); no task was sent")
            request("thread/inject_items", {
                "threadId": thread_id,
                "items": [{"type": "message", "role": "developer", "content": [{
                    "type": "input_text",
                    "text": "This conversation is coordinated by Claude for the user. No coding task has run yet. Await the next task message.",
                }]}],
            })
            request("thread/name/set", {"threadId": thread_id, "name": title})
            request("thread/unsubscribe", {"threadId": thread_id})
            if not rollout.is_file():
                raise BridgeError("The new conversation was not persisted; no coding turn was started")
        finally:
            with contextlib.suppress(OSError):
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdout.close()


class Desktop:
    def __init__(self, log):
        self.log = log
        self.sock = None
        self.buffer = bytearray()
        self.client_id = "initializing-client"
        self.thread_id = None
        self.owner = None
        self.state = None
        self.revision = -1
        self.snapshot_generation = 0
        self.allow_recent_history = False
        self.recent_history_only = False

    def __enter__(self):
        override = os.environ.get("CODEX_WORKER_DESKTOP_SOCKET")
        paths = [Path(override)] if override else [codex_home() / "ipc/ipc.sock", Path(f"/tmp/codex-ipc/ipc-{os.getuid()}.sock")]
        errors = []
        for path in paths:
            connection = None
            try:
                info = path.stat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise BridgeError("IPC socket is not owned by the current user")
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(5)
                connection.connect(str(path))
                if hasattr(socket, "SO_PEERCRED"):
                    _, peer_uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if peer_uid != os.getuid():
                        raise BridgeError("IPC peer belongs to a different user")
                self.sock = connection
                self.client_id = self.request("initialize", {"clientType": "claude-codex-worker"}, timeout=5)["result"]["clientId"]
                return self
            except (OSError, ValueError, BridgeError) as exc:
                errors.append(f"{path}: {exc}")
                if connection is not None:
                    connection.close()
                self.sock = None
                self.buffer.clear()
        raise BridgeError("Cannot connect to the running desktop; no CLI fallback. " + "; ".join(errors))

    def __exit__(self, *_):
        if self.sock is not None:
            if self.owner:
                with contextlib.suppress(OSError, BridgeError):
                    self.follow(False)
            self.sock.close()

    def send(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        if len(raw) > MAX_FRAME:
            raise BridgeError("IPC frame too large")
        self.sock.sendall(struct.pack("<I", len(raw)) + raw)

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buffer) >= 4:
                size = struct.unpack("<I", self.buffer[:4])[0]
                if size > MAX_FRAME:
                    raise BridgeError("Invalid desktop IPC frame length")
                if len(self.buffer) >= size + 4:
                    raw = bytes(self.buffer[4:4 + size])
                    del self.buffer[:4 + size]
                    return json.loads(raw)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.sock], [], [], remaining)[0]:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise BridgeError("Desktop IPC disconnected; the desktop turn may still be running. Use watch, never resubmit.")
            self.buffer.extend(chunk)

    def handle(self, message):
        kind = message.get("type")
        if kind == "client-discovery-request":
            self.send({"type": "client-discovery-response", "requestId": message["requestId"], "response": {"canHandle": False}})
        elif kind == "request":
            self.send({"type": "response", "requestId": message["requestId"], "resultType": "error", "error": "no-handler"})
        elif kind == "broadcast":
            params = message.get("params", {})
            if params.get("conversationId") != self.thread_id or params.get("hostId", "local") != "local":
                return
            if message.get("sourceClientId") != self.owner:
                return
            log_event(self.log, {"direction": "in", **message})
            method = message.get("method")
            if method in VERSIONS and message.get("version", 0) != VERSIONS[method]:
                raise BridgeError(f"Unsupported desktop broadcast version for {method}")
            if method == "thread-stream-following-status-requested":
                self.follow(True)
            if method == "thread-stream-state-changed":
                change = params.get("change", {})
                if change.get("type") == "snapshot":
                    state = change["conversationState"]
                    if state.get("id") != self.thread_id:
                        raise BridgeError("Desktop snapshot belongs to a different conversation")
                    self.state, self.revision = state, change["revision"]
                    self.snapshot_generation += 1
                # Patches are logged. Request native snapshots for status instead
                # of implementing the desktop's evolving text/Immer patch format.

    def request(self, method, params, timeout=30, target=None):
        identifier = str(uuid.uuid4())
        message = {"type": "request", "requestId": identifier, "sourceClientId": self.client_id,
                   "version": VERSIONS.get(method, 0), "method": method, "params": params,
                   "timeoutMs": int(timeout * 1000)}
        if target:
            message["targetClientId"] = target
        log_event(self.log, {"direction": "out", **message})
        self.send(message)
        deadline = time.monotonic() + timeout + 2
        while time.monotonic() < deadline:
            response = self.receive(max(0, deadline - time.monotonic()))
            if response is None:
                break
            if response.get("type") == "response" and response.get("requestId") == identifier:
                log_event(self.log, {"direction": "in", **response})
                if response.get("resultType") != "success":
                    raise BridgeError(f"{method}: {response.get('error', 'unknown IPC error')}")
                if target and response.get("handledByClientId") != target:
                    raise BridgeError("Desktop response came from a different owner")
                return response
            self.handle(response)
        raise BridgeError(f"{method}: response timed out; a mutating request must NOT be sent again blindly")

    def attach(self, thread_id):
        self.thread_id = str(uuid.UUID(thread_id))
        opened = subprocess.run(["xdg-open", f"codex://threads/{self.thread_id}"], capture_output=True, timeout=15)
        if opened.returncode:
            raise BridgeError("Could not open the Codex desktop conversation: " + opened.stderr.decode(errors="replace")[:1000])
        deadline = time.monotonic() + 40
        while True:
            try:
                reply = self.request("thread-owner-discovery", {"hostId": "local", "conversationId": self.thread_id}, timeout=5)
                self.owner = reply["handledByClientId"]
                break
            except BridgeError as exc:
                if "no-client-found" not in str(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        self.follow(True)
        return self.snapshot()

    def follow(self, following):
        self.send({"type": "broadcast", "method": "thread-stream-following-changed",
                   "version": VERSIONS["thread-stream-following-changed"], "sourceClientId": self.client_id,
                   "targetClientIds": [self.owner],
                   "params": {"conversationId": self.thread_id, "hostId": "local", "following": following}})

    def snapshot(self):
        if self.recent_history_only:
            return self.current_snapshot()
        try:
            result = self.request("thread-follower-load-complete-history", {"conversationId": self.thread_id}, target=self.owner)["result"]
        except BridgeError as exc:
            # This pinned Desktop can call an unsupported item-pagination method
            # after reopening a legacy thread. Preserve its honest history
            # boundaries and read a fresh owner snapshot, never import a rollout
            # or present the loaded tail as complete. Other failures stay closed.
            if not self.allow_recent_history or str(exc) != (
                    "thread-follower-load-complete-history: thread/items/list is not supported yet"):
                raise
            self.recent_history_only = True
            return self.current_snapshot()
        revision = result["revision"]
        self.follow(True)  # Even an existing follower gets a full current snapshot.
        deadline = time.monotonic() + 15
        while self.state is None or self.revision < revision:
            message = self.receive(max(0, deadline - time.monotonic()))
            if message is None:
                raise BridgeError("No complete desktop snapshot arrived; execution status is unknown")
            self.handle(message)
        return self.state

    def current_snapshot(self):
        generation = self.snapshot_generation
        self.follow(True)
        deadline = time.monotonic() + 15
        while self.snapshot_generation == generation:
            message = self.receive(max(0, deadline - time.monotonic()))
            if message is None:
                raise BridgeError("No fresh desktop snapshot arrived; execution status is unknown")
            self.handle(message)
        return self.state

    def start(self, prompt, client_message_id):
        result = self.request("thread-follower-start-turn", {
            "conversationId": self.thread_id,
            "turnStart": {"request": {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "clientUserMessageId": client_message_id,
                "multiAgentMode": "explicitRequestOnly",
            }, "context": {"inheritThreadSettings": True}},
        }, target=self.owner)["result"]["result"]
        turn_id = result["turn"]["id"]
        if not isinstance(turn_id, str) or not turn_id:
            raise BridgeError("Desktop did not return a verified turn ID")
        return turn_id

    def interrupt(self, turn_id):
        if not turn_id:
            raise BridgeError("Refusing to interrupt without the exact original turn ID")
        return self.request("thread-follower-interrupt-turn", {
            "conversationId": self.thread_id, "expectedTurnId": turn_id, "mode": "user-stop",
        }, target=self.owner)["result"]

    def pump(self, timeout=1):
        message = self.receive(timeout)
        if message is not None:
            self.handle(message)


def turns(state):
    result = {}
    history = state.get("turnHistory", {}).get("history", {}) if state.get("turnHistory") else {}
    for turn in list(history.get("entitiesByKey", {}).values()) + state.get("turns", []):
        if isinstance(turn, dict) and turn.get("turnId"):
            result[turn["turnId"]] = turn
    return list(result.values())
