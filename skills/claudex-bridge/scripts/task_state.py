"""Durable, local state for the two fixed main agents (Python 3.9+, stdlib).

``begin_attempt`` must return before the adapter sends its one request. It first
fsyncs an unknown, unresolved submission and its client message ID. Recovery only
binds observed IDs; this module never sends, retries, or treats EOF as completion.
Use ``with store.locked(owner):`` around an entire dispatch/monitor operation to
retain the old worker's process lock. Individual writes also acquire that lock.

Evidence references contain a local ``path`` and its ``sha256``. A terminal
reference additionally carries ``event_type``, ``session_id``, ``execution_id``,
``id_kind`` and ``status`` from the native adapter. Hashes prove which saved bytes
were used, not that the adapter classified them correctly, that tests are useful,
or that an agent actually read a change. Actor identities are explicit caller
assertions, not authentication. The caller must verify the live Git snapshot
immediately before review; this state layer does not inspect a worktree.

Old task.json files remain visible to the unresolved guard but cannot be silently
converted to this schema. No state is read or created merely by importing here.
"""
import contextlib
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
import uuid

try:
    import fcntl
except ImportError:  # Importable by portable doctor; execution fails closed.
    fcntl = None


SCHEMA = 1
BACKEND = "native-shared-v1"
TERMINAL = {"completed", "failed", "interrupted", "aborted"}
PRODUCTS = {"claude", "codex"}


class StateError(ValueError):
    """A requested transition or its evidence does not satisfy the contract."""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir(path):
    if path.is_dir():
        return
    _mkdir(path.parent)
    path.mkdir(exist_ok=True, mode=0o700)
    _fsync_directory(path.parent)


def state_root():
    default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    root = Path(os.environ.get("CODEX_WORKER_STATE_DIR", str(default / "claude-codex-worker")))
    root = root.expanduser().resolve()
    _mkdir(root)
    return root


def task_dir(root, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task_id):
        raise StateError("task ID must be 1–80 letters, digits, underscores or hyphens")
    return Path(root) / task_id


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise StateError("Task metadata must be an object")
    return value


def _save(path, data):
    # Replace metadata, NEVER worker.lock: every runner must lock the same inode.
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextlib.contextmanager
def worker_lock(root, owner=None):
    """Use exactly the retained Linux worker's flock and stable lock-file inode."""
    if fcntl is None:
        raise RuntimeError("The shared worker lock requires fcntl; this platform is not enabled")
    with (Path(root) / "worker.lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            raise RuntimeError("A runner already holds worker.lock. Owner: " + lock.read()[:500]) from None
        if owner is not None:
            # Serialize before changing the diagnostic owner record.
            encoded = json.dumps(owner, ensure_ascii=False, allow_nan=False)
            lock.seek(0)
            lock.truncate()
            lock.write(encoded)
            lock.flush()
            os.fsync(lock.fileno())
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def assert_no_unresolved(root):
    """Include legacy records, so losing either runner cannot permit a duplicate."""
    for metadata in Path(root).glob("*/task.json"):
        other = read_json(metadata)
        if other.get("unresolved") or other.get("status") == "running":
            raise RuntimeError("Task %s has an unresolved execution; reconnect without resending" %
                               other.get("task_id", metadata.parent.name))


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise StateError(label + " must be a nonempty string")
    return value


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise StateError(label + " must be a lowercase SHA256 digest")
    return value


def _identity(value, allow_unbound=False):
    if not isinstance(value, dict) or set(value) != {"product", "session_id"}:
        raise StateError("Identity requires exactly product and session_id")
    if value["product"] not in PRODUCTS:
        raise StateError("Product must be claude or codex")
    if not (allow_unbound and value["session_id"] is None):
        _text(value["session_id"], "session_id")
    return copy.deepcopy(value)


def evidence_ref(path, **metadata):
    """Reference already-saved bytes; the caller remains responsible for meaning."""
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    if not raw:
        raise StateError("Evidence file must not be empty")
    return dict(metadata, path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def _evidence(value):
    if not isinstance(value, dict):
        raise StateError("Evidence must be a local path and SHA256 reference")
    path = Path(_text(value.get("path"), "evidence path")).expanduser().resolve()
    expected = _digest(value.get("sha256"), "evidence sha256")
    raw = path.read_bytes()
    if not raw or hashlib.sha256(raw).hexdigest() != expected:
        raise StateError("Evidence file is empty or its SHA256 changed")
    result = copy.deepcopy(value)
    result["path"] = str(path)
    # Fail before mutating task data if metadata is not JSON serializable.
    json.dumps(result, allow_nan=False)
    return result


def _paths(values):
    if not isinstance(values, (list, tuple)):
        raise StateError("Paths must be a list of project-relative strings")
    paths = set()
    for value in values:
        _text(value, "path")
        if PurePosixPath(value).is_absolute() or any(part in ("", ".", "..", ".git")
                                                    for part in value.split("/")):
            raise StateError("Path must stay inside the project and exclude Git internals")
        paths.add(value)
    return sorted(paths)


def _execution(session_id, execution_id, id_kind):
    if id_kind not in ("turn", "message"):
        raise StateError("id_kind must be turn or message")
    return {"session_id": _text(session_id, "session_id"),
            "execution_id": _text(execution_id, "execution_id"), "id_kind": id_kind}


class TaskStore:
    def __init__(self, root=None):
        self.root = Path(root).expanduser().resolve() if root is not None else state_root()
        _mkdir(self.root)
        self._mutex = threading.RLock()
        self._lock_depth = 0
        self._lock_pid = None

    @contextlib.contextmanager
    def locked(self, owner=None):
        """Reentrant only for this store; other stores/old workers must contend."""
        with self._mutex:
            if self._lock_depth:
                if self._lock_pid != os.getpid():
                    raise RuntimeError("A worker lock inherited across fork cannot be reused")
                self._lock_depth += 1
                try:
                    yield self
                finally:
                    self._lock_depth -= 1
            else:
                with worker_lock(self.root, owner):
                    self._lock_pid = os.getpid()
                    self._lock_depth = 1
                    try:
                        yield self
                    finally:
                        self._lock_depth = 0
                        self._lock_pid = None

    def read(self, task_id):
        data = read_json(task_dir(self.root, task_id) / "task.json")
        if data.get("schema_version") != SCHEMA or data.get("backend") != BACKEND:
            raise StateError("Legacy or unsupported task schema; no automatic migration is allowed")
        if data.get("task_id") != task_id:
            raise StateError("Task ID differs from its directory")
        initiator = _identity(data.get("initiator"))
        executor = _identity(data.get("executor"), allow_unbound=True)
        if (initiator["product"] == executor["product"] or
                data.get("direction") != initiator["product"] + "-to-" + executor["product"]):
            raise StateError("Saved main-agent direction is invalid")
        requirements = data.get("requirements", {})
        if _hash(_text(requirements.get("text"), "requirements")) != requirements.get("hash"):
            raise StateError("Saved requirements hash differs from its text")
        return data

    def _save(self, data):
        data["updated_at"] = now()
        _save(task_dir(self.root, data["task_id"]) / "task.json", data)
        return copy.deepcopy(data)

    @staticmethod
    def _actor(data, actor):
        if _identity(actor) != data["initiator"]:
            raise StateError("Only the fixed initiating session may change requirements or review")

    @staticmethod
    def _attempt(data, number):
        if (type(number) is not int or number < 1 or not data["attempts"] or
                number != data["attempts"][-1]["number"]):
            raise StateError("Operation must name the exact latest attempt")
        return data["attempts"][-1]

    @staticmethod
    def _invalidate_reviews(data, reason):
        for review in data["reviews"]:
            if review.get("active"):
                review.update(active=False, invalidated_at=now(), invalidation_reason=reason)
        data.pop("review", None)

    def create_task(self, task_id, project, initiator, executor, requirements):
        initiator = _identity(initiator)
        executor = _identity(executor, allow_unbound=True)
        if initiator["product"] == executor["product"]:
            raise StateError("The two main agents must use different products")
        project = Path(project).expanduser().resolve()
        if not project.is_dir():
            raise StateError("Project directory does not exist")
        _text(requirements, "requirements")
        directory = task_dir(self.root, task_id)
        with self.locked():
            if (directory / "task.json").exists():
                data = self.read(task_id)
                if (data["project"] != str(project) or data["initiator"] != initiator or
                        data["executor"] != executor or data["requirements"]["text"] != requirements):
                    raise StateError("Existing task identity, project and requirements cannot be replaced")
                return data
            if directory.exists():
                raise StateError("Task directory already exists without valid task metadata")
            _mkdir(directory)
            initial = {"version": 1, "hash": _hash(requirements), "text": requirements,
                       "updated_at": now(), "actor": initiator}
            return self._save({"schema_version": SCHEMA, "backend": BACKEND, "task_id": task_id,
                              "project": str(project), "initiator": initiator, "executor": executor,
                              "direction": initiator["product"] + "-to-" + executor["product"],
                              "created_at": now(), "status": "ready", "unresolved": False,
                              "requirements": initial, "requirements_history": [copy.deepcopy(initial)],
                              "attempts": [], "reviews": []})

    def bind_executor(self, task_id, session_id):
        _text(session_id, "session_id")
        with self.locked():
            data = self.read(task_id)
            previous = data["executor"]["session_id"]
            if previous is not None and previous != session_id:
                raise StateError("Executor session is fixed and cannot be replaced")
            if previous == session_id:
                return data
            data["executor"]["session_id"] = session_id
            return self._save(data)

    def update_requirements(self, task_id, requirements, *, actor):
        _text(requirements, "requirements")
        with self.locked():
            data = self.read(task_id)
            self._actor(data, actor)
            updated = {"version": data["requirements"]["version"] + 1,
                       "hash": _hash(requirements), "text": requirements,
                       "updated_at": now(), "actor": copy.deepcopy(actor)}
            data["requirements"] = updated
            data["requirements_history"].append(copy.deepcopy(updated))
            self._invalidate_reviews(data, "requirements_updated")
            # Feedback cannot claim that an already-submitted execution stopped.
            if not data["unresolved"]:
                data["status"] = "revision_requested" if data["attempts"] else "ready"
            return self._save(data)

    def begin_attempt(self, task_id, *, actor):
        with self.locked():
            data = self.read(task_id)
            self._actor(data, actor)
            assert_no_unresolved(self.root)
            if (data["status"] == "needs_review" and data["attempts"] and
                    data["attempts"][-1]["requirements_version"] == data["requirements"]["version"]):
                raise StateError("The initiating agent must review the delivery before rework")
            used_ids = {attempt.get("client_message_id")
                        for path in self.root.glob("*/task.json")
                        for attempt in read_json(path).get("attempts", [])}
            for _ in range(10):
                message_id = str(uuid.uuid4())
                if message_id not in used_ids:
                    break
            else:
                raise StateError("Could not allocate a unique client_message_id")
            number = len(data["attempts"]) + 1
            _mkdir(task_dir(self.root, task_id) / ("attempt-%03d" % number))
            attempt = {"number": number, "directory": "attempt-%03d" % number,
                       "kind": "start" if number == 1 else "resume", "started_at": now(),
                       "client_message_id": message_id, "submission_state": "unknown",
                       "execution": None, "turn_id": None, "turn_completed": False,
                       "requirements_version": data["requirements"]["version"],
                       "requirements_hash": data["requirements"]["hash"]}
            data["attempts"].append(attempt)
            self._invalidate_reviews(data, "new_attempt")
            data.update(status="submitting", unresolved=True)
            return self._save(data)

    def bind_execution(self, task_id, attempt_number, session_id, execution_id, id_kind,
                       *, client_message_id):
        identity = _execution(session_id, execution_id, id_kind)
        with self.locked():
            data = self.read(task_id)
            attempt = self._attempt(data, attempt_number)
            if client_message_id != attempt["client_message_id"]:
                raise StateError("Execution does not match the saved client_message_id")
            if data["executor"]["session_id"] not in (None, session_id):
                raise StateError("Execution belongs to a different executor session")
            if attempt["execution"] is not None:
                if attempt["execution"] != identity:
                    raise StateError("The original execution ID cannot be replaced")
                return data
            if not data["unresolved"] or attempt["submission_state"] != "unknown":
                raise StateError("Only an unresolved submission may bind its original execution")
            data["executor"]["session_id"] = session_id
            attempt.update(execution=identity, submission_state="started", bound_at=now())
            attempt["turn_id" if id_kind == "turn" else "message_id"] = execution_id
            data["status"] = "running"
            return self._save(data)

    def recover_execution(self, task_id, attempt_number, candidates):
        """Bind one observed match; zero or ambiguous matches leave state unchanged."""
        with self.locked():
            data = self.read(task_id)
            attempt = self._attempt(data, attempt_number)
            if not isinstance(candidates, (list, tuple)):
                raise StateError("Recovery requires observed execution candidates")
            matches = [item for item in candidates if isinstance(item, dict) and
                       item.get("client_message_id") == attempt["client_message_id"]]
            if len(matches) != 1:
                raise StateError("Recovery requires exactly one matching client_message_id; do not resend")
            match = matches[0]
            return self.bind_execution(task_id, attempt_number, match.get("session_id"),
                                       match.get("execution_id"), match.get("id_kind"),
                                       client_message_id=attempt["client_message_id"])

    def mark_detached(self, task_id, attempt_number, reason):
        _text(reason, "reason")
        with self.locked():
            data = self.read(task_id)
            attempt = self._attempt(data, attempt_number)
            if not data["unresolved"]:
                raise StateError("A resolved execution cannot become detached")
            attempt.update(error=reason, monitor_stopped_at=now())
            data["status"] = "detached"
            return self._save(data)

    def observe_terminal(self, task_id, attempt_number, session_id, execution_id, id_kind,
                         *, status, evidence):
        identity = _execution(session_id, execution_id, id_kind)
        if status not in TERMINAL:
            raise StateError("A real terminal protocol event is required; EOF is not completion")
        with self.locked():
            data = self.read(task_id)
            attempt = self._attempt(data, attempt_number)
            if attempt["execution"] != identity or data["executor"]["session_id"] != session_id:
                raise StateError("Terminal event does not match the exact bound execution")
            reference = _evidence(evidence)
            expected_type = {"codex": {"task_complete", "codex.task_complete"},
                             "claude": {"turn.complete", "claude.turn.complete"}}[data["executor"]["product"]]
            if data["executor"]["product"] == "codex" and status != "completed":
                expected_type.add("codex_desktop_terminal")
            if reference.get("event_type") not in expected_type:
                raise StateError("Evidence must reference the executor's native terminal event")
            if any(reference.get(key) != value for key, value in dict(identity, status=status).items()):
                raise StateError("Terminal evidence identity or status does not match")
            if status == "completed" and (reference.get("error") or reference.get("aborted") or
                    reference.get("reason") in ("error", "failed", "failure", "aborted", "interrupted",
                                                 "cancelled", "canceled")):
                raise StateError("An error or aborted event cannot be successful completion")
            if attempt["submission_state"] == "terminal":
                if attempt["terminal_status"] != status or attempt["completion_evidence"] != reference:
                    raise StateError("The original terminal observation cannot be replaced")
                return data
            if not data["unresolved"]:
                raise StateError("Terminal observation requires an unresolved execution")
            attempt.update(submission_state="terminal", terminal_status=status, ended_at=now(),
                           completion_evidence=reference, turn_completed=(status == "completed"))
            attempt.pop("error", None)
            data.update(unresolved=False, status={"completed": "needs_review", "aborted": "interrupted"}.get(status, status))
            return self._save(data)

    def record_delivery(self, task_id, attempt_number, snapshot_hash, changed_paths, evidence):
        _digest(snapshot_hash, "snapshot_hash")
        paths = _paths(changed_paths)
        with self.locked():
            data = self.read(task_id)
            attempt = self._attempt(data, attempt_number)
            if data["unresolved"] or not attempt.get("turn_completed"):
                raise StateError("Delivery requires the exact execution's verified successful terminal event")
            reference = _evidence(evidence)
            _evidence(attempt["completion_evidence"])
            delivery = {"snapshot_hash": snapshot_hash, "changed_paths": paths, "evidence": reference}
            if attempt.get("delivery") == delivery:
                return data
            attempt.setdefault("delivery_history", []).append(copy.deepcopy(delivery))
            attempt["delivery"] = delivery
            self._invalidate_reviews(data, "delivery_updated")
            data["status"] = "needs_review"
            return self._save(data)

    def record_review(self, task_id, *, actor, decision, attempt_number, requirements_hash,
                      snapshot_hash, reviewed_paths, test_evidence, notes):
        if decision not in ("pass", "revise", "blocked"):
            raise StateError("Review decision must be pass, revise or blocked")
        _text(notes, "review notes")
        paths = _paths(reviewed_paths)
        with self.locked():
            data = self.read(task_id)
            self._actor(data, actor)
            attempt = self._attempt(data, attempt_number)
            delivery = attempt.get("delivery")
            if data["unresolved"] or not attempt.get("turn_completed") or not delivery:
                raise StateError("Review requires a resolved, successful execution and delivery evidence")
            if (requirements_hash != attempt["requirements_hash"] or
                    requirements_hash != data["requirements"]["hash"] or
                    attempt["requirements_version"] != data["requirements"]["version"]):
                raise StateError("Review does not match the current requirements version and hash")
            if snapshot_hash != delivery["snapshot_hash"]:
                raise StateError("Review does not match the final delivery snapshot hash")
            _evidence(attempt["completion_evidence"])
            _evidence(delivery["evidence"])
            if not isinstance(test_evidence, (list, tuple)):
                raise StateError("test_evidence must be a list of evidence references")
            tests = []
            for value in test_evidence:
                reference = _evidence(value)
                _text(reference.get("command"), "test command")
                if type(reference.get("exit_code")) is not int:
                    raise StateError("Test evidence must include an observed integer exit_code")
                tests.append(reference)
            if decision == "pass":
                if not set(delivery["changed_paths"]).issubset(paths):
                    raise StateError("Pass requires review coverage of every final changed path")
                if not tests or any(item["exit_code"] != 0 for item in tests):
                    raise StateError("Pass requires successful test evidence")
            self._invalidate_reviews(data, "new_review")
            review = {"id": str(uuid.uuid4()), "decision": decision, "actor": copy.deepcopy(actor),
                      "attempt": attempt_number, "requirements_version": data["requirements"]["version"],
                      "requirements_hash": requirements_hash, "snapshot_hash": snapshot_hash,
                      "reviewed_paths": paths, "test_evidence": tests, "notes": notes,
                      "active": True, "at": now()}
            data["reviews"].append(review)
            data["review"] = copy.deepcopy(review)
            data["status"] = {"pass": "accepted", "revise": "revision_requested", "blocked": "blocked"}[decision]
            return self._save(data)
