"""One existing Codex Desktop owner, with durable exact-turn recovery.

The caller holds the shared worker lock and persists its unresolved attempt
BEFORE submit. ``directory`` belongs to that one attempt, not the whole task.
Construction, context entry and preflight do not create files. Entering uses the
retained adapter's version check and attach (which opens that native thread).
Only submit and interrupt send execution-control requests. No session, bootstrap
server, model process or fallback executor is created here.

An IPC acknowledgment is not completion. Successful completion requires both a
fresh Desktop terminal snapshot and a complete, exact-turn rollout task_complete
with a nonempty result. Evidence is a private immutable copy of those actual
observations. Read failures and partial records return unknown, never success.

Rollouts are bounded to 256 MiB, individual JSONL records to 16 MiB. Every read
checks the pinned file identity and the entire previously observed byte prefix,
including any partial final line. This intentionally favors conservative
recovery over accepting a replaced, truncated or rewritten transcript. Only
regular files under the shared CODEX_HOME/sessions directory are accepted.
"""

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import codex_processes
from desktop_bridge import BridgeError, Desktop, turns, validate_rollout, verify_installation


MAX_ROLLOUT = 256 * 1024 * 1024
MAX_LINE = 16 * 1024 * 1024
MAX_METADATA = 2 * 1024 * 1024
TERMINAL = {"completed", "failed", "interrupted"}


class OwnerError(BridgeError):
    """The exact owner/attempt cannot safely perform the requested operation."""


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise OwnerError(label + " must be a nonempty string")
    return value


def _identifier(value, label):
    value = _text(value, label)
    if len(value) > 256:
        raise OwnerError(label + " exceeds the identity limit")
    return value


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       allow_nan=False, indent=2) + "\n").encode("utf-8")


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_file(path, limit):
    """Read one stable regular file; never block on a FIFO or follow a final link."""
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise OwnerError("Expected a regular file owned by the current user: " + str(path))
        if before.st_size > limit:
            raise OwnerError("Evidence file exceeds the supported size limit: " + str(path))
        chunks, remaining = [], before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OwnerError("File was truncated while reading: " + str(path))
            chunks.append(chunk)
            remaining -= len(chunk)
        after, current = os.fstat(descriptor), path.lstat()
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in stable):
            raise OwnerError("File changed while reading; retry observation without resending")
        if any(getattr(current, key) != getattr(after, key) for key in stable):
            raise OwnerError("File was replaced or changed while reading: " + str(path))
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _read_json(path, limit=MAX_METADATA):
    raw, _info = _read_file(path, limit)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise OwnerError("Saved owner state must be a JSON object")
    return value


def _write(path, value, *, exclusive=False):
    raw = _encoded(value)
    temporary = path if exclusive else path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if not exclusive:
            temporary.unlink(missing_ok=True)


class _DeferredLog:
    """Preflight leaves no files; activated execution diagnostics stay private."""

    def __init__(self):
        self.stream = None

    def activate(self, directory):
        if self.stream is not None:
            return
        descriptor = os.open(str(directory / "codex-desktop.jsonl"),
                             os.O_WRONLY | os.O_CREAT | os.O_APPEND |
                             getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            os.close(descriptor)
            raise OwnerError("Desktop diagnostic log is not a private regular file")
        self.stream = os.fdopen(descriptor, "a", encoding="utf-8")

    def write(self, text):
        if self.stream is not None:
            return self.stream.write(text)
        return len(text)

    def flush(self):
        if self.stream is not None:
            self.stream.flush()

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class CodexOwner:
    def __init__(self, session_id, rollout_path, directory, *, expected_project=None):
        try:
            canonical = str(uuid.UUID(session_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise OwnerError("Expected an exact Codex session UUID") from exc
        if canonical != session_id:
            raise OwnerError("Session UUID must use canonical lowercase spelling")
        self.session_id = canonical
        self.expected_project = None
        if expected_project is not None:
            project = Path(expected_project)
            if not project.is_absolute() or not project.is_dir():
                raise OwnerError("Expected project must be an existing absolute directory")
            self.expected_project = project.resolve()
        self.rollout_path = validate_rollout(rollout_path)
        self.directory = Path(directory).expanduser().resolve()
        self._state_path = self.directory / "codex-owner.json"
        self._record = None
        self._source = None
        self._desktop = None
        self._log = _DeferredLog()

    def __enter__(self):
        if self._desktop is not None:
            raise OwnerError("CodexOwner context is already connected")
        verify_installation()
        self._load()
        # Verify the rollout before attach can open a desktop conversation.
        self._rollout()
        desktop = Desktop(self._log)
        try:
            self._desktop = desktop.__enter__()
            # Exact-turn matching below remains mandatory when the native client
            # exposes only recent history. An absent old turn stays unknown.
            self._desktop.allow_recent_history = True
            self._check_snapshot(self._desktop.attach(self.session_id))
        except BaseException:
            with contextlib.suppress(Exception):
                desktop.__exit__(None, None, None)
            self._desktop = None
            self._log.close()
            raise
        return self

    def __exit__(self, *args):
        try:
            if self._desktop is not None:
                self._desktop.__exit__(*args)
        finally:
            self._desktop = None
            self._log.close()

    def _connected(self):
        if self._desktop is None:
            raise OwnerError("Use CodexOwner as a connected context manager")

    def _load(self):
        # The caller holds the shared lock. In particular, never replace this
        # connection's newer in-memory prefix pin with older on-disk bytes after
        # a read-only entry/preflight observed an append.
        if self._record is not None:
            return
        if not self._state_path.exists():
            return
        record = _read_json(self._state_path)
        if (record.get("schema") != 1 or record.get("session_id") != self.session_id or
                record.get("rollout_path") != str(self.rollout_path)):
            raise OwnerError("Attempt directory belongs to a different session or rollout")
        _identifier(record.get("client_message_id"), "saved client_message_id")
        if record.get("execution_id") is not None:
            _identifier(record["execution_id"], "saved execution_id")
        if record.get("submission_state") not in ("unknown", "started", "terminal"):
            raise OwnerError("Unsupported saved submission state")
        source = record.get("source")
        if (not isinstance(source, dict) or any(type(source.get(key)) is not int or source[key] < 0
                for key in ("device", "inode", "size")) or
                not isinstance(source.get("sha256"), str) or len(source["sha256"]) != 64):
            raise OwnerError("Invalid saved rollout identity or prefix digest")
        self._record = record
        self._source = source

    def _activate(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OwnerError("Attempt directory is writable by another user")
        self._log.activate(self.directory)

    def _save(self):
        _write(self._state_path, self._record)

    def _reserve(self, client_message_id, *, prompt=None):
        self._activate()
        record = {"schema": 1, "session_id": self.session_id,
                  "rollout_path": str(self.rollout_path), "source": self._source,
                  "client_message_id": client_message_id, "execution_id": None,
                  "submission_state": "unknown", "dispatch_offset": None}
        if prompt is not None:
            record.update(prompt_sha256=_sha(prompt.encode("utf-8")),
                          dispatch_offset=self._source["size"],
                          process_baseline=codex_processes.baseline())
        # Never overwrite a reservation written by a competing/stale controller.
        _write(self._state_path, record, exclusive=True)
        self._record = record

    def _rollout(self):
        raw, info = _read_file(self.rollout_path, MAX_ROLLOUT)
        previous = self._source
        if previous is not None:
            if (info.st_dev, info.st_ino) != (previous["device"], previous["inode"]):
                raise OwnerError("Rollout file was replaced; completion is unknown")
            if len(raw) < previous["size"]:
                raise OwnerError("Rollout was truncated; completion is unknown")
            if _sha(raw[:previous["size"]]) != previous["sha256"]:
                raise OwnerError("Previously observed rollout bytes were rewritten")
        records, offset = [], 0
        for line in raw.splitlines(keepends=True):
            if len(line) > MAX_LINE:
                raise OwnerError("Rollout record exceeds the supported size limit")
            if not line.endswith(b"\n"):
                # A writer can be between writes. Never parse or accept half a record.
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise OwnerError("Malformed complete rollout record") from exc
            if not isinstance(event, dict):
                raise OwnerError("Rollout record is not an object")
            records.append((offset, line, event))
            offset += len(line)
        if (not records or records[0][2].get("type") != "session_meta" or
                not isinstance(records[0][2].get("payload"), dict) or
                records[0][2]["payload"].get("id") != self.session_id):
            raise OwnerError("Rollout header does not identify the exact Codex session")
        if any(event.get("type") == "session_meta" for _start, _raw, event in records[1:]):
            raise OwnerError("Rollout contains more than one session header")
        self._source = {"device": info.st_dev, "inode": info.st_ino,
                        "size": len(raw), "sha256": _sha(raw)}
        if self._record is not None:
            self._record["source"] = self._source
        return records

    def _check_snapshot(self, state):
        if not isinstance(state, dict) or state.get("id") != self.session_id:
            raise OwnerError("Desktop snapshot belongs to a different session")
        if self.expected_project is not None:
            cwd = state.get("cwd")
            if (not isinstance(cwd, str) or not Path(cwd).is_absolute() or
                    Path(cwd).resolve() != self.expected_project):
                raise OwnerError("Desktop owner working directory does not match the task project")
        if not isinstance(state.get("turns", []), list) or not isinstance(state.get("requests", []), list):
            raise OwnerError("Unsupported Desktop snapshot shape")
        values = turns(state)
        if any(turn.get("status") not in TERMINAL | {"inProgress"} for turn in values):
            raise OwnerError("Unsupported Desktop turn status; execution is unknown")
        return values

    def _snapshot(self):
        self._connected()
        state = self._desktop.snapshot()
        self._check_snapshot(state)
        return state

    def preflight(self):
        """Check the existing owner is idle. Does not submit or write state/logs."""
        state = self._snapshot()
        if any(turn.get("status") == "inProgress" for turn in turns(state)) or state.get("requests"):
            raise OwnerError("The exact Desktop session has an in-progress turn or pending request")
        self._rollout()
        return {"ready": True, "session_id": self.session_id, "rollout_path": str(self.rollout_path)}

    def submit(self, prompt, client_message_id):
        """Send once. Any exception after reservation requires observe, never resend."""
        self._connected()
        _text(prompt, "prompt")
        _identifier(client_message_id, "client_message_id")
        # Also notice a reservation that appeared after a fresh context entered.
        self._load()
        if self._record is not None:
            raise OwnerError("This attempt was already reserved; observe it without resending")
        state = self._snapshot()
        if any(turn.get("status") == "inProgress" for turn in turns(state)) or state.get("requests"):
            raise OwnerError("The exact Desktop session is not idle; no request was sent")
        if self._matches(state, client_message_id):
            raise OwnerError("client_message_id already exists in this session; observe without resending")
        self._rollout()
        self._reserve(client_message_id, prompt=prompt)
        turn_id = _identifier(self._desktop.start(prompt, client_message_id), "returned turn ID")
        self._record.update(execution_id=turn_id, submission_state="started")
        self._save()
        return turn_id

    @staticmethod
    def _matches(state, client_message_id):
        return [turn for turn in turns(state) if isinstance(turn.get("params"), dict) and
                turn["params"].get("clientUserMessageId") == client_message_id]

    def _find(self, state, client_message_id, execution_id):
        bound = self._record.get("execution_id")
        if bound:
            if execution_id is not None and bound != execution_id:
                raise OwnerError("Requested turn differs from the original bound turn")
            return next((turn for turn in turns(state) if turn["turnId"] == bound), None)
        matches = self._matches(state, client_message_id)
        if len(matches) != 1:
            return None
        turn = matches[0]
        if execution_id is not None and turn["turnId"] != execution_id:
            raise OwnerError("Recovered turn differs from the requested exact turn")
        self._record.update(execution_id=_identifier(turn["turnId"], "recovered turn ID"),
                            submission_state="started")
        self._save()
        return turn

    def _unknown(self, reason):
        return {"execution_id": self._record.get("execution_id") if self._record else None,
                "id_kind": "turn", "state": "unknown", "terminal_status": None,
                "evidence": None, "result": "", "error": str(reason)}

    def _evidence(self, turn, completion):
        status, turn_id = turn["status"], turn["turnId"]
        event_type = "task_complete" if status == "completed" else "codex_desktop_terminal"
        identity = {"session_id": self.session_id, "execution_id": turn_id,
                    "id_kind": "turn", "status": status, "event_type": event_type}
        path = self.directory / "codex-terminal.json"
        if path.exists():
            saved = _read_json(path, MAX_ROLLOUT)
            if (saved.get("schema") != 1 or any(saved.get(key) != value for key, value in identity.items()) or
                    saved.get("client_message_id") != self._record["client_message_id"] or
                    saved.get("desktop", {}).get("turn", {}).get("turnId") != turn_id or
                    saved.get("desktop", {}).get("turn", {}).get("status") != status):
                raise OwnerError("Saved terminal evidence conflicts with the exact execution")
            if completion is not None and saved.get("rollout", {}).get("raw_line_sha256") != _sha(completion[1]):
                raise OwnerError("Saved terminal evidence conflicts with the rollout completion")
        else:
            saved = dict(identity, schema=1, client_message_id=self._record["client_message_id"],
                         desktop={"session_id": self.session_id,
                                  "owner_client_id": getattr(self._desktop, "owner", None),
                                  "revision": getattr(self._desktop, "revision", None),
                                  "turn": copy.deepcopy(turn)})
            if completion is not None:
                offset, raw, event = completion
                saved["rollout"] = {"path": str(self.rollout_path), "device": self._source["device"],
                                    "inode": self._source["inode"], "offset": offset,
                                    "raw_line_sha256": _sha(raw), "event": event}
            _write(path, saved, exclusive=True)
        raw, _info = _read_file(path, MAX_ROLLOUT)
        reference = dict(identity, path=str(path), sha256=_sha(raw))
        previous = self._record.get("terminal_evidence")
        if previous is not None and previous != reference:
            raise OwnerError("Previously saved terminal evidence changed")
        self._record.update(submission_state="terminal", terminal_evidence=reference)
        self._save()
        return reference

    def observe(self, client_message_id, execution_id=None):
        """Observe/recover the original request; never send or retry a prompt."""
        self._connected()
        _identifier(client_message_id, "client_message_id")
        if execution_id is not None:
            _identifier(execution_id, "execution_id")
        self._load()
        if self._record is not None:
            if self._record["client_message_id"] != client_message_id:
                raise OwnerError("client_message_id differs from this attempt's original message")
            if self._record.get("execution_id") and execution_id not in (None, self._record["execution_id"]):
                raise OwnerError("Requested turn differs from the original bound turn")
        self._activate()
        if self._record is None:
            # Even a recovery-only attempt may never later submit this message.
            self._reserve(client_message_id)
        try:
            records = self._rollout()
            self._save()
            state = self._snapshot()
            turn = self._find(state, client_message_id, execution_id)
            if turn is None:
                return self._unknown("No unique original turn is currently observable; do not resend")
            status = turn["status"]
            result = {"execution_id": turn["turnId"], "id_kind": "turn", "state": "running",
                      "terminal_status": None, "evidence": None, "result": ""}
            if status == "inProgress":
                requests = [request for request in state.get("requests", []) if not isinstance(request, dict) or
                            not isinstance(request.get("params"), dict) or
                            request["params"].get("turnId") in (None, turn["turnId"])]
                result["state"] = "waiting" if requests else "running"
                return result
            completion = None
            if status == "completed":
                matches = [(offset, raw, event) for offset, raw, event in records
                           if event.get("type") == "event_msg" and isinstance(event.get("payload"), dict) and
                           event["payload"].get("type") == "task_complete" and
                           event["payload"].get("turn_id") == turn["turnId"]]
                if len(matches) != 1:
                    return self._unknown("Expected one complete rollout task_complete for the exact turn")
                completion = matches[0]
                offset, _raw, event = completion
                payload = event["payload"]
                text = payload.get("last_agent_message")
                cutoff = self._record.get("dispatch_offset")
                if cutoff is not None and offset < cutoff:
                    return self._unknown("Completion predates this attempt's original submission")
                if (not isinstance(text, str) or not text.strip() or payload.get("error") or
                        payload.get("aborted") or payload.get("isAborted") or
                        payload.get("status", "completed") != "completed" or
                        payload.get("reason") in {"error", "failed", "failure", "aborted", "interrupted",
                                                  "cancelled", "canceled"}):
                    return self._unknown("Exact rollout completion has no successful, complete result")
                result["result"] = text
            if self._record.get("process_baseline") is not None:
                remaining = codex_processes.candidates(self.session_id, self._record["process_baseline"])
                if remaining:
                    return self._unknown("Native turn ended but attributed command processes remain active")
            if (self._record.get("interrupt_requested") and
                    self._record.get("command_cleanup", {}).get("complete") is not True):
                if self._record.get("process_baseline") is None:
                    return self._unknown("Native turn ended but its command cleanup is unconfirmed")
                self._record["command_cleanup"] = {"complete": True, "remaining": [],
                                                   "basis": "observed_no_remaining_attributed_processes"}
                self._save()
            result.update(state="terminal", terminal_status=status, evidence=self._evidence(turn, completion))
            return result
        except (BridgeError, codex_processes.ProcessError, OSError, ValueError, KeyError, TypeError) as exc:
            return self._unknown(exc)

    def interrupt(self, client_message_id, execution_id):
        """Interrupt only the still-running, durably bound original turn once."""
        _identifier(execution_id, "execution_id")
        observation = self.observe(client_message_id, execution_id)
        if (observation["execution_id"] != execution_id or
                observation["state"] not in ("running", "waiting")):
            raise OwnerError("Interrupt requires the exact original turn to be observably in progress")
        if self._record.get("interrupt_requested"):
            raise OwnerError("Interrupt was already requested; observe without repeating it")
        # The retained protocol's expectedTurnId additionally checks server-side
        # that a newly started native turn cannot be interrupted by this request.
        targets = codex_processes.candidates(self.session_id, self._record.get("process_baseline"))
        with codex_processes.PinnedProcesses(targets) as commands:
            # Recheck the actual turn after enumerating processes, before either
            # execution-control action. Never send an unscoped cleanup interrupt.
            current = self.observe(client_message_id, execution_id)
            if current["state"] not in ("running", "waiting"):
                raise OwnerError("Original turn changed before interruption")
            self._record.update(interrupt_requested=True,
                                command_cleanup={"complete": False, "targets": targets})
            self._save()
            try:
                reply = self._desktop.interrupt(execution_id)
            finally:
                self._record["command_cleanup"] = commands.terminate()
                remaining = codex_processes.candidates(self.session_id, self._record["process_baseline"])
                self._record["command_cleanup"].update(complete=not remaining, remaining=remaining)
                self._save()
        return {"execution_id": execution_id, "id_kind": "turn", "interrupt_requested": True,
                "reply": reply, "command_cleanup": self._record["command_cleanup"]}
