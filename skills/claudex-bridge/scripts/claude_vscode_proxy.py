#!/usr/bin/env python3
"""Read-only stdio follower for the Claude Code 2.1.295 VS Code SDK.

The native extension supplies the original CLI path as argv[1]. We inspect its
package version but never execute it. An explicit binding selects one existing
transcript or a recording of the owner's stream-json output. See
docs/claude-vscode-protocol.md for the protocol evidence and integration limits.
"""

import hashlib
import json
import os
from pathlib import Path
import select
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


SUPPORTED_VERSION = "2.1.295"
BINDING_ENV = "CLAUDEX_BRIDGE_CLAUDE_BINDING"
MAX_LINE_BYTES = 16 * 1024 * 1024
READ_BYTES = 64 * 1024
VIEW_TYPES = frozenset({
    "assistant", "auth_status", "command_lifecycle", "conversation_reset",
    "prompt_suggestion", "rate_limit_event", "result", "stream_event",
    "system", "tool_progress", "tool_use_summary", "user",
})


class ProtocolError(Exception):
    """A binding or protocol mismatch; the follower must stop, not reconnect."""


def require_uuid(value: Any, label: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ProtocolError("{} must be a canonical UUID".format(label)) from None
    return value


def read_object(data: bytes, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("{} is not valid UTF-8 JSON".format(label)) from None
    if not isinstance(value, dict):
        raise ProtocolError("{} must be a JSON object".format(label))
    return value


def open_regular(path: Path) -> int:
    """Keep the opened inode; never follow a replaced/symlinked source."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ProtocolError("Binding and source must be regular files")
        fd = os.open(str(path), flags)
    except OSError:
        raise ProtocolError("Cannot open the explicitly bound file") from None
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise ProtocolError("Bound file changed while opening")
    return fd


@dataclass(frozen=True)
class Binding:
    session_id: str
    source: Path
    source_format: str
    claude_code_version: str
    history: str = "native"
    start_offset: Optional[int] = None
    ready_file: Optional[Path] = None
    owner_state_dir: Optional[Path] = None

    @classmethod
    def load(cls, path: str, check_ready_file: bool = True) -> "Binding":
        binding_path = Path(path)
        if not binding_path.is_absolute():
            raise ProtocolError("Binding path must be absolute")
        fd = open_regular(binding_path)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_LINE_BYTES + 1)
        if len(data) > MAX_LINE_BYTES:
            raise ProtocolError("Binding is too large")
        obj = read_object(data, "Binding")
        if obj.get("schema") != 1 or type(obj.get("schema")) is not int:
            raise ProtocolError("Unsupported binding schema")
        session_id = require_uuid(obj.get("session_id"), "session_id")
        source_value = obj.get("source")
        if not isinstance(source_value, str) or not Path(source_value).is_absolute():
            raise ProtocolError("source must be an absolute existing path")
        fmt = obj.get("source_format")
        if fmt not in ("transcript", "stream-json"):
            raise ProtocolError("source_format must be transcript or stream-json")
        version = obj.get("claude_code_version")
        if version != SUPPORTED_VERSION:
            raise ProtocolError("Unsupported producer version; expected " + SUPPORTED_VERSION)
        history = obj.get("history", "native")
        if history not in ("native", "replay"):
            raise ProtocolError("history must be native or replay")
        offset = obj.get("start_offset")
        if offset is not None and (type(offset) is not int or offset < 0 or history != "native"):
            raise ProtocolError("start_offset requires native history and a nonnegative integer")
        if history == "native" and offset is None:
            raise ProtocolError("Native history requires its exact start_offset; an implicit tail could lose events")
        ready = obj.get("ready_file")
        if ready is not None:
            if not isinstance(ready, str) or not Path(ready).is_absolute():
                raise ProtocolError("ready_file must be an absolute new file path")
            if (check_ready_file and os.path.lexists(ready)) or not Path(ready).parent.is_dir():
                raise ProtocolError("ready_file must not already exist and its parent must exist")
        owner = obj.get("owner_state_dir")
        if owner is not None and (not isinstance(owner, str) or not Path(owner).is_absolute()
                                  or not Path(owner).is_dir()):
            raise ProtocolError("owner_state_dir must be an absolute existing owner directory")
        return cls(session_id, Path(source_value), fmt, version, history, offset,
                   Path(ready) if ready is not None else None,
                   Path(owner) if owner is not None else None)


def validate_invocation(argv: List[str], binding: Binding, config_probe: bool = False) -> None:
    """Recognize the actual process-wrapper calling convention, without spawning."""
    if not argv or not Path(argv[0]).is_absolute() or not Path(argv[0]).is_file():
        raise ProtocolError("Expected the original bundled Claude executable as first argument")
    executable = Path(argv[0]).resolve()
    package = None
    for parent in list(executable.parents)[:4]:
        candidate = parent / "package.json"
        if candidate.is_file():
            package = read_object(candidate.read_bytes(), "Extension package")
            break
    if (package is None or package.get("name") != "claude-code"
            or not isinstance(package.get("publisher"), str)
            or package["publisher"].lower() != "anthropic"
            or package.get("version") != SUPPORTED_VERSION):
        raise ProtocolError("Only the official Claude Code VS Code extension " + SUPPORTED_VERSION + " is supported")
    args = argv[1:]
    for forbidden in ("--continue", "-c", "--fork-session", "--session-id", "--resume-session-at", "--from-pr"):
        if any(arg == forbidden or arg.startswith(forbidden + "=") for arg in args):
            raise ProtocolError("Follower rejects alternate session selection: " + forbidden)
    values = {}
    for flag in ("--resume", "--input-format", "--output-format"):
        found = []
        for index, arg in enumerate(args):
            if arg == flag:
                if index + 1 >= len(args):
                    raise ProtocolError("Missing argument for " + flag)
                found.append(args[index + 1])
            elif arg.startswith(flag + "="):
                found.append(arg.split("=", 1)[1])
        if flag == "--resume" and config_probe:
            if found:
                raise ProtocolError("A metadata probe must not select a conversation")
            continue
        if len(found) != 1:
            raise ProtocolError("Follower requires exactly one " + flag)
        values[flag] = found[0]
    if not config_probe and values["--resume"] != binding.session_id:
        raise ProtocolError("Requested session does not match the explicit binding")
    if values["--input-format"] != "stream-json" or values["--output-format"] != "stream-json":
        raise ProtocolError("Follower requires the SDK stream-json transport")


class JsonlTail:
    """Incrementally decode complete JSONL records, keeping a partial UTF-8 line."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = open_regular(path)
        info = os.fstat(self.fd)
        self.identity = (info.st_dev, info.st_ino)
        self.offset = 0
        self.pending = b""
        self.digest = hashlib.sha256()

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read_available(self) -> List[Tuple[int, Dict[str, Any]]]:
        try:
            info = self.path.lstat()
        except OSError:
            raise ProtocolError("Bound source disappeared; refusing an implicit reconnect") from None
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
            raise ProtocolError("Bound source was replaced; refusing an implicit reconnect")
        if info.st_size < self.offset:
            raise ProtocolError("Bound source was truncated")
        if self.offset:
            # A truncate-and-rewrite can restore the previous length between
            # polls, including within one filesystem timestamp tick. Verify the
            # consumed prefix even at EOF. This development adapter favors
            # explicit consistency checks over large-history performance.
            # pread leaves the tail cursor unchanged.
            prefix = hashlib.sha256()
            checked = 0
            while checked < self.offset:
                chunk = os.pread(self.fd, min(READ_BYTES, self.offset - checked), checked)
                if not chunk:
                    raise ProtocolError("Bound source changed while verifying its history")
                prefix.update(chunk)
                checked += len(chunk)
            if prefix.digest() != self.digest.digest():
                raise ProtocolError("Bound source history was rewritten in place")
        end = info.st_size  # A finite snapshot even while the owner is writing.
        result = []
        while self.offset < end:
            chunk = os.read(self.fd, min(READ_BYTES, end - self.offset))
            if not chunk:
                raise ProtocolError("Bound source changed while reading")
            self.offset += len(chunk)
            self.digest.update(chunk)
            self.pending += chunk
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                if len(line) > MAX_LINE_BYTES:
                    raise ProtocolError("Source JSONL record is too large")
                if line.strip():
                    record_end = self.offset - len(self.pending)
                    result.append((record_end, read_object(line, "Source record")))
            if len(self.pending) > MAX_LINE_BYTES:
                raise ProtocolError("Source JSONL record is too large")
        return result


def transcript_message(row: Dict[str, Any], session_id: str) -> Optional[Dict[str, Any]]:
    """The native extension's sb4 mapping, limited to main user/assistant rows."""
    if row.get("type") not in ("user", "assistant") or row.get("isSidechain") or row.get("teamName"):
        return None
    message = row.get("message")
    if (not isinstance(message, dict) or message.get("role") != row["type"]
            or not isinstance(message.get("content"), (str, list))):
        raise ProtocolError("Malformed transcript message")
    mapped = {
        "type": row["type"], "message": message,
        "uuid": require_uuid(row.get("uuid"), "Message uuid"),
        "session_id": session_id, "parent_tool_use_id": None,
    }
    if isinstance(row.get("timestamp"), str):
        mapped["timestamp"] = row["timestamp"]
    if row["type"] == "user":
        if row.get("origin") is not None:
            mapped["origin"] = row["origin"]
        if isinstance(row.get("toolDenialUnanswered"), dict):
            mapped["toolDenialUnanswered"] = row["toolDenialUnanswered"]
        for key in ("interruptedByShutdown", "isCompactSummary", "isCompletedLocalCommand"):
            if row.get(key) is True:
                mapped[key] = True
        if row.get("isMeta") is True:
            mapped["isSynthetic"] = True
    return mapped


class Follower:
    def __init__(self, binding: Binding, config_probe: bool = False):
        self.binding = binding
        self.config_probe = config_probe
        self.tail = JsonlTail(binding.source)
        self.initialized = False
        self.known_session = False
        self.known_version = False
        self.seen = {}
        self.init_frame = None
        self.replay = []
        self.transcript_parents = {}
        self.host_initialized = False
        self.ready_written = False
        try:
            rows = self.tail.read_available()
            cutoff = binding.start_offset
            if binding.history == "native" and cutoff is None:
                raise ProtocolError("Native history requires its exact start_offset")
            if cutoff is not None:
                if cutoff > self.tail.offset or (cutoff and os.pread(self.tail.fd, 1, cutoff - 1) != b"\n"):
                    raise ProtocolError("start_offset must be an existing JSONL boundary")
            for end, row in rows:
                emit = not config_probe and (binding.history == "replay" or (cutoff is not None and end > cutoff))
                frame = self.accept(row, replaying=emit)
                if emit and frame is not None:
                    self.replay.append(frame)
            if not self.known_session or not self.known_version:
                raise ProtocolError("Source does not prove the bound session and supported producer version")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.tail.close()

    def accept(self, row: Dict[str, Any], replaying: bool = False) -> Optional[Dict[str, Any]]:
        transcript = self.binding.source_format == "transcript"
        session_id = row.get("sessionId" if transcript else "session_id")
        kind = row.get("type")
        display = kind in ("user", "assistant") if transcript else kind in VIEW_TYPES
        if session_id is not None and session_id != self.binding.session_id:
            raise ProtocolError("Source contains a different session")
        if display and session_id != self.binding.session_id:
            raise ProtocolError("Display event has no bound session identity")
        if session_id == self.binding.session_id:
            self.known_session = True
        version = row.get("version") if transcript else (row.get("claude_code_version") if kind == "system" and row.get("subtype") == "init" else None)
        if version is not None:
            if version != SUPPORTED_VERSION:
                raise ProtocolError("Source producer version changed or is unsupported")
            self.known_version = True
        if transcript:
            # Native history handles forks/compaction. This minimal live adapter
            # cannot retract prior rows when a branch or compacted chain changes.
            if row.get("subtype") == "compact_boundary" and (self.initialized or replaying):
                raise ProtocolError("Transcript compaction needs the native history loader or stream-json source")
            frame = transcript_message(row, self.binding.session_id)
            if frame is None:
                return None
        else:
            if not display:
                # In particular never deliver the owner's permission/hook RPCs
                # to a second SDK host. They belong to the executing process.
                return None
            frame = row
            require_uuid(frame.get("uuid"), "Event uuid")
            if kind in ("user", "assistant"):
                message = frame.get("message")
                if (not isinstance(message, dict) or message.get("role") != kind
                        or not isinstance(message.get("content"), (str, list))):
                    raise ProtocolError("Malformed SDK message")
                if "parent_tool_use_id" not in frame:
                    raise ProtocolError("SDK message lacks parent_tool_use_id")
            if kind == "system" and row.get("subtype") == "init":
                self.init_frame = row
        key = frame["uuid"]
        encoded = hashlib.sha256(json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8")).digest()
        if key in self.seen:
            if self.seen[key] != encoded:
                raise ProtocolError("An existing event UUID changed; refusing an ambiguous replay")
            return None
        if transcript:
            parent = row.get("parentUuid")
            previous = self.transcript_parents.get(parent)
            message = frame["message"]
            same_assistant = (previous is not None and previous[1] == "assistant"
                              and frame["type"] == "assistant" and message.get("id")
                              and previous[2] == message["id"])
            tool_result = (frame["type"] == "user" and isinstance(message.get("content"), list)
                           and all(isinstance(block, dict) and block.get("type") == "tool_result"
                                   for block in message["content"]) and bool(message["content"]))
            if (previous is not None and previous[0] != frame["uuid"] and not same_assistant
                    and not tool_result and (self.initialized or replaying)):
                raise ProtocolError("Transcript branch changes require native history reload")
            self.transcript_parents[parent] = (frame["uuid"], frame["type"], message.get("id"))
        self.seen[key] = encoded
        return frame

    def poll(self) -> List[Dict[str, Any]]:
        if not self.initialized or self.config_probe:
            return []
        frames = []
        for _, row in self.tail.read_available():
            frame = self.accept(row)
            if frame is not None:
                frames.append(frame)
        return frames

    def control(self, frame: Dict[str, Any]) -> List[Dict[str, Any]]:
        kind = frame.get("type")
        if kind in ("keep_alive", "control_cancel_request"):
            return []
        if kind == "user":
            raise ProtocolError("Read-only follower refused a user message; send it to the executing owner")
        if kind != "control_request":
            raise ProtocolError("Unsupported inbound SDK message type")
        request_id, request = frame.get("request_id"), frame.get("request")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256 or not isinstance(request, dict):
            raise ProtocolError("Malformed control request")
        subtype = request.get("subtype")
        if not isinstance(subtype, str):
            raise ProtocolError("Control request has no subtype")
        if subtype != "initialize":
            if subtype == "get_settings" and self.initialized and not self.config_probe:
                # The native extension calls this only after its initialize
                # promise resolves. This is stronger evidence than stdout flush.
                self.host_initialized = True
            return [{"type": "control_response", "response": {
                "subtype": "error", "request_id": request_id,
                "error": "Read-only follower: {} is unavailable; use the executing owner".format(subtype),
                "error_code": "read_only_follower",
            }}]
        # This is the real CLI's outbound-only REPL initialize shape. These are
        # the follower's available capabilities, not guessed owner settings.
        response = {
            "commands": [], "agents": [], "output_style": "normal",
            "available_output_styles": ["normal"], "models": [], "account": {},
            "pid": os.getpid(), "remote_control_available": False,
            "remote_control_auto_enable": False, "remote_control_auto_on_by_default": False,
        }
        if request.get("hooks"):
            response["hooks_applied"] = False
        if request.get("plugins"):
            response["plugins_applied"] = False
        if "workspaceTrust" in request:
            response["workspace_trust_recorded"] = False
        if self.init_frame is not None:
            if isinstance(self.init_frame.get("model"), str):
                response["current_model"] = self.init_frame["model"]
            if isinstance(self.init_frame.get("permissionMode"), str):
                response["current_permission_mode"] = self.init_frame["permissionMode"]
        result = [{"type": "control_response", "response": {
            "subtype": "success", "request_id": request_id, "response": response,
        }}]
        if not self.initialized:
            self.initialized = True
            if self.config_probe:
                return result
            result.extend(self.replay)
            self.replay = []
            result.append({
                "type": "system", "subtype": "informational", "level": "notice",
                "content": "Claudex-Bridge: read-only follower attached. Send prompts and controls to the executing owner.",
                "session_id": self.binding.session_id, "uuid": str(uuid.uuid4()),
            })
        return result

    def write_ready(self) -> None:
        """Publish an atomic, non-overwriting handshake receipt, not UI/liveness."""
        path = self.binding.ready_file
        if path is None or self.config_probe or not self.host_initialized or self.ready_written:
            return
        receipt = {
            "schema": 1, "stage": "host_initialize_observed",
            "evidence": "get_settings_after_initialize", "session_id": self.binding.session_id,
            "claude_code_version": SUPPORTED_VERSION, "pid": os.getpid(),
            "source_device": self.tail.identity[0], "source_inode": self.tail.identity[1],
            "start_offset": self.binding.start_offset, "created_at": time.time(),
        }
        fd, temporary = tempfile.mkstemp(prefix=".claudex-ready-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(receipt, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, str(path))  # Atomic and refuses a stale receipt.
            self.ready_written = True
        finally:
            os.unlink(temporary)


def serve(follower: Follower, input_fd: int, output: Any) -> None:
    pending = b""

    def write(frames: List[Dict[str, Any]]) -> None:
        for frame in frames:
            output.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
        if frames:
            output.flush()

    while True:
        ready, _, _ = select.select([input_fd], [], [], 0.1)
        if ready:
            data = os.read(input_fd, READ_BYTES)
            if not data:
                if pending.strip():
                    raise ProtocolError("SDK input ended with an incomplete JSONL record")
                return
            pending += data
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if len(line) > MAX_LINE_BYTES:
                    raise ProtocolError("SDK input record is too large")
                if line.strip():
                    write(follower.control(read_object(line, "SDK input")))
            if len(pending) > MAX_LINE_BYTES:
                raise ProtocolError("SDK input record is too large")
        write(follower.poll())
        follower.write_ready()


def main(argv: Optional[List[str]] = None) -> int:
    follower = None
    try:
        path = os.environ.get(BINDING_ENV)
        if not path:
            raise ProtocolError(BINDING_ENV + " must name an explicit existing-session binding")
        config_probe = os.environ.get("CLAUDE_CODE_CONFIG_PROBE") == "1"
        binding = Binding.load(path, check_ready_file=not config_probe)
        validate_invocation(sys.argv[1:] if argv is None else argv, binding, config_probe=config_probe)
        follower = Follower(binding, config_probe=config_probe)
        serve(follower, sys.stdin.fileno(), sys.stdout)
        return 0
    except (ProtocolError, OSError) as exc:
        print("claudex-bridge follower: " + str(exc), file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if follower is not None:
            follower.close()


if __name__ == "__main__":
    sys.exit(main())
