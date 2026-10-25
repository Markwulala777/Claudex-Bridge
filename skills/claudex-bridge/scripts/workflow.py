"""Bounded, evidence-backed orchestration of two existing native main sessions.

Adapters are context managers supplied by ``adapter_factory(task, transport,
attempt_directory)``. They preflight the fixed session, submit once, and observe
an exact execution using the CodexOwner normalized observation shape. Watching
never submits. This module performs no native discovery or model calls itself.

Review records explicit initiating-session assertions and complete evidence
coverage; it cannot prove that an agent understood the changes. A live worktree
check immediately precedes acceptance, but unrelated processes can still write
after that check. Tests are actual argv processes with saved output and exit code.
"""
import contextlib
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time
import uuid

import git_evidence
from task_state import StateError, TaskStore, assert_no_unresolved, evidence_ref, now, task_dir


class WorkflowError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, value):
    raw = _canonical(value) + b"\n"
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read(path):
    with Path(path).open(encoding="utf-8") as stream:
        result = json.load(stream)
    if not isinstance(result, dict):
        raise WorkflowError("Expected a saved JSON object")
    return result


def _reference(value):
    if not isinstance(value, dict) or not isinstance(value.get("path"), str):
        raise WorkflowError("Missing saved evidence reference")
    actual = evidence_ref(value["path"])
    if actual["sha256"] != value.get("sha256"):
        raise WorkflowError("Saved workflow evidence changed")
    return _read(actual["path"])


def _seconds(value, label, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < 0 or (not allow_zero and value == 0)):
        raise WorkflowError(label + " must be a finite positive duration")
    return float(value)


def _transport(value, product):
    kind, path_key = {"codex": ("codex-desktop", "rollout_path"),
                      "claude": ("claude-native", "owner_state_dir")}[product]
    if not isinstance(value, dict) or set(value) != {"kind", path_key} or value.get("kind") != kind:
        raise WorkflowError("Transport must match the fixed executor: " + kind)
    path = value[path_key]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise WorkflowError("Transport requires an absolute " + path_key)
    return {"kind": kind, path_key: str(Path(path).resolve())}


def _binding_hash(binding):
    return hashlib.sha256(_canonical({key: binding[key] for key in
        ("schema_version", "task_id", "project", "initiator", "executor", "transport", "baseline")})).hexdigest()


class Workflow:
    def __init__(self, root=None, adapter_factory=None):
        self.store = TaskStore(root)
        self.adapter_factory = adapter_factory

    def _directory(self, task_id):
        return task_dir(self.store.root, task_id)

    def _binding(self, task):
        binding = _read(self._directory(task["task_id"]) / "workflow.json")
        if binding.get("schema_version") != 1 or binding.get("binding_hash") != _binding_hash(binding):
            raise WorkflowError("Workflow binding is incomplete or changed")
        for key in ("task_id", "project", "initiator", "executor"):
            if binding.get(key) != task[key]:
                raise WorkflowError("Workflow's fixed " + key + " changed")
        if _transport(binding["transport"], task["executor"]["product"]) != binding["transport"]:
            raise WorkflowError("Workflow transport changed")
        baseline = _reference(binding["baseline"])
        if not baseline.get("complete") or baseline.get("project") != task["project"]:
            raise WorkflowError("A complete baseline for this project is required")
        return binding

    @staticmethod
    def _actor(task, actor):
        if actor != task["initiator"]:
            raise WorkflowError("Only the fixed initiating session may dispatch, test or review")

    def _adapter(self, task, binding, attempt_directory):
        if self.adapter_factory is None:
            raise WorkflowError("An exact native adapter factory is required")
        return self.adapter_factory(copy.deepcopy(task), copy.deepcopy(binding["transport"]), attempt_directory)

    def create(self, task_id, project, initiator, executor, requirements, transport):
        if not isinstance(executor, dict) or not executor.get("session_id"):
            raise WorkflowError("Bind an existing executor session before creating a workflow")
        transport = _transport(transport, executor["product"])
        with self.store.locked({"task_id": task_id, "action": "create", "runner_pid": os.getpid()}):
            task = self.store.create_task(task_id, project, initiator, executor, requirements)
            directory = self._directory(task_id)
            path = directory / "workflow.json"
            if path.exists():
                existing = _read(path)
                if existing.get("transport") != transport:
                    raise WorkflowError("Existing workflow transport cannot be replaced")
                if existing.get("baseline"):
                    self._binding(task)
                    return self.status(task_id)
            else:
                existing = {"schema_version": 1, "task_id": task_id, "project": task["project"],
                            "initiator": task["initiator"], "executor": task["executor"],
                            "transport": transport, "baseline": None, "created_at": now()}
                _write(path, existing)
            baseline_dir = directory / "baseline"
            if (baseline_dir / "manifest.json").exists():
                baseline = _read(baseline_dir / "manifest.json")
            else:
                baseline = git_evidence.capture(task["project"], baseline_dir)
            if not baseline.get("complete") or not git_evidence.compare(baseline, baseline)["complete"]:
                raise WorkflowError("Initial Git baseline is incomplete; dispatch is blocked")
            existing["baseline"] = evidence_ref(baseline_dir / "manifest.json")
            existing["binding_hash"] = _binding_hash(existing)
            _write(path, existing)
            return self.status(task_id)

    def status(self, task_id):
        task = self.store.read(task_id)
        binding = self._binding(task)
        result = copy.deepcopy(task)
        result["workflow"] = {"transport": binding["transport"], "baseline": binding["baseline"],
                              "task_directory": str(self._directory(task_id)),
                              "baseline_snapshot_hash": _reference(binding["baseline"])["snapshot_hash"]}
        result.update(acceptance_current=False, review_stale=False)
        if task["attempts"]:
            attempt = task["attempts"][-1]
            result["workflow"]["delivery_packet"] = (attempt.get("delivery") or {}).get("evidence")
            directory = self._directory(task_id) / attempt["directory"]
            result["workflow"]["test_records"] = self._tests(directory)
            request = directory / "stop-requested.json"
            if request.exists():
                result["workflow"]["stop_requested"] = _read(request)
            if task["status"] == "accepted":
                _attempt, _packet, final = self._delivery(task)
                review = task.get("review", {})
                current = (review.get("active") and review.get("decision") == "pass"
                           and review.get("attempt") == attempt["number"]
                           and review.get("requirements_version") == task["requirements"]["version"]
                           and review.get("requirements_hash") == task["requirements"]["hash"]
                           and review.get("snapshot_hash") == final["snapshot_hash"]
                           and git_evidence.verify_snapshot(task["project"], final)["matches"])
                result.update(acceptance_current=bool(current), review_stale=not current)
                if not current:
                    # Preserve the historical review in task.json, while avoiding
                    # a claim that the current worktree is still accepted.
                    result.update(recorded_status=task["status"], status="review_stale")
        return result

    @staticmethod
    def _prompt(task):
        prompt = ("You are the main %s executor. The fixed initiating main agent is %s, session %s.\n"
                "Preserve existing user work and follow the project AGENTS.md. Implement the requirements "
                "and report actual changes, test commands/results, and unresolved issues. "
                "Do not recursively call the bridge, change the two main roles, or approve your own work. "
                "A Codex executor may delegate independent subtasks, integrate them and verify the result; "
                "Claude subagents are off by default. Execution completion is not final acceptance.\n\n"
                "Requirements version %s (SHA256 %s):\n%s" %
                (task["executor"]["product"], task["initiator"]["product"], task["initiator"]["session_id"],
                 task["requirements"]["version"], task["requirements"]["hash"], task["requirements"]["text"]))
        latest_review = task["reviews"][-1] if task["reviews"] else None
        if latest_review and latest_review["decision"] == "revise":
            prompt += ("\n\nThe initiating agent rejected attempt %s and requests this rework. "
                       "Address these concrete acceptance findings in the same executor session.\n"
                       "Reviewed paths: %s\nReview notes:\n%s" %
                       (latest_review["attempt"], json.dumps(latest_review["reviewed_paths"], ensure_ascii=False),
                        latest_review["notes"]))
        return prompt

    def dispatch(self, task_id, *, actor, wait_seconds=45, poll_interval=0.2):
        wait_seconds = _seconds(wait_seconds, "wait_seconds", allow_zero=True)
        poll_interval = _seconds(poll_interval, "poll_interval")
        with self.store.locked({"task_id": task_id, "action": "dispatch", "runner_pid": os.getpid()}):
            task = self.store.read(task_id)
            self._actor(task, actor)
            binding = self._binding(task)
            assert_no_unresolved(self.store.root)
            directory = self._directory(task_id) / ("attempt-%03d" % (len(task["attempts"]) + 1))
            with self._adapter(task, binding, directory) as adapter:
                ready = adapter.preflight()
                if (not isinstance(ready, dict) or ready.get("session_id") != task["executor"]["session_id"]
                        or ready.get("ready") is False or ready.get("status", "idle") != "idle"):
                    raise WorkflowError("Preflight did not confirm the fixed executor session is idle")
                task = self.store.begin_attempt(task_id, actor=actor)
                attempt = task["attempts"][-1]
                try:
                    # Return values differ by native product. Binding always comes
                    # from observe(), which must match the saved client message ID.
                    adapter.submit(self._prompt(task), attempt["client_message_id"])
                except Exception as exc:
                    self.store.mark_detached(task_id, attempt["number"], "Submission outcome unknown: " + str(exc))
                except BaseException:
                    self.store.mark_detached(task_id, attempt["number"], "Submission interrupted; reconnect without resending")
                    raise
                self._monitor(task_id, adapter, wait_seconds, poll_interval)
            return self.status(task_id)

    def watch(self, task_id, *, wait_seconds=45, poll_interval=0.2):
        wait_seconds = _seconds(wait_seconds, "wait_seconds", allow_zero=True)
        poll_interval = _seconds(poll_interval, "poll_interval")
        with self.store.locked({"task_id": task_id, "action": "watch", "runner_pid": os.getpid()}):
            task = self.store.read(task_id)
            binding = self._binding(task)
            if not task["attempts"]:
                return self.status(task_id)
            attempt = task["attempts"][-1]
            if not task["unresolved"]:
                if attempt.get("turn_completed") and not attempt.get("delivery"):
                    self._capture_delivery(task, binding)
                return self.status(task_id)
            with self._adapter(task, binding, self._directory(task_id) / attempt["directory"]) as adapter:
                self._monitor(task_id, adapter, wait_seconds, poll_interval)
            return self.status(task_id)

    def _monitor(self, task_id, adapter, wait_seconds, poll_interval):
        deadline = time.monotonic() + wait_seconds
        while True:
            task = self.store.read(task_id)
            attempt = task["attempts"][-1]
            identity = attempt.get("execution")
            try:
                observation = adapter.observe(attempt["client_message_id"],
                                              identity["execution_id"] if identity else None)
                if not isinstance(observation, dict):
                    raise WorkflowError("Native observation must be an object")
                if observation.get("session_id", task["executor"]["session_id"]) != task["executor"]["session_id"]:
                    raise WorkflowError("Observation belongs to another session")
                if observation.get("execution_id"):
                    task = self.store.bind_execution(task_id, attempt["number"], task["executor"]["session_id"],
                                                    observation["execution_id"], observation.get("id_kind"),
                                                    client_message_id=attempt["client_message_id"])
                    attempt = task["attempts"][-1]
                    identity = attempt["execution"]
                state = observation.get("state")
                if state == "terminal":
                    if not identity:
                        raise WorkflowError("Terminal observation has no exact execution identity")
                    task = self.store.observe_terminal(task_id, attempt["number"], **identity,
                                                       status=observation.get("terminal_status"),
                                                       evidence=observation.get("evidence"))
                    if task["attempts"][-1]["turn_completed"]:
                        self._capture_delivery(task, self._binding(task), observation.get("result", ""))
                    return
                if state not in ("running", "waiting", "unknown"):
                    raise WorkflowError("Unrecognized native state; completion was not inferred")
                self._apply_stop(task, adapter)
                if state == "waiting":
                    self.store.mark_detached(task_id, attempt["number"], "Native executor is awaiting input or permission")
                    return
            except Exception as exc:
                current = self.store.read(task_id)
                if current["unresolved"]:
                    self.store.mark_detached(task_id, attempt["number"], str(exc) or "Native observation failed")
                    return
                raise
            except BaseException:
                current = self.store.read(task_id)
                if current["unresolved"]:
                    self.store.mark_detached(task_id, attempt["number"], "Monitoring interrupted; execution remains unresolved")
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.store.mark_detached(task_id, attempt["number"],
                                         observation.get("error") or "Bounded monitoring ended; watch the original execution")
                return
            time.sleep(min(poll_interval, remaining))

    def _capture_delivery(self, task, binding, result=""):
        attempt = task["attempts"][-1]
        if not attempt.get("turn_completed") or task["unresolved"]:
            raise WorkflowError("Only a successful resolved execution can produce delivery evidence")
        baseline = _reference(binding["baseline"])
        previous = baseline
        for earlier in reversed(task["attempts"]):
            if earlier.get("delivery"):
                previous = _reference(_reference(earlier["delivery"]["evidence"])["snapshot"])
                break
        directory = self._directory(task["task_id"]) / attempt["directory"] / ("delivery-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        _sync_directory(directory.parent)
        final = git_evidence.capture(task["project"], directory / "snapshot", baseline=previous)
        comparison = git_evidence.compare(baseline, final)
        if not final["complete"] or not comparison["complete"]:
            raise WorkflowError("Final Git snapshot or comparison is incomplete; acceptance is blocked")
        packet = {"schema_version": 1, "task_id": task["task_id"], "project": task["project"],
                  "attempt_number": attempt["number"], "requirements_hash": attempt["requirements_hash"],
                  "requirements_version": attempt["requirements_version"], "baseline": binding["baseline"],
                  "snapshot": evidence_ref(directory / "snapshot/manifest.json"),
                  "comparison": comparison, "executor_result": result, "created_at": now()}
        _write(directory / "packet.json", packet)
        paths = sorted({path for change in comparison["changes"]
                        for path in (change["before_path"], change["path"])})
        return self.store.record_delivery(task["task_id"], attempt["number"], final["snapshot_hash"], paths,
                                          evidence_ref(directory / "packet.json"))

    def feedback(self, task_id, requirements, *, actor):
        with self.store.locked():
            task = self.store.read(task_id)
            self._binding(task)
            self.store.update_requirements(task_id, requirements, actor=actor)
            return self.status(task_id)

    def refresh_delivery(self, task_id, *, actor):
        """Capture deliberate post-execution changes; prior acceptance/tests expire."""
        with self.store.locked():
            task = self.store.read(task_id)
            self._actor(task, actor)
            self._capture_delivery(task, self._binding(task))
            return self.status(task_id)

    def _delivery(self, task):
        if not task["attempts"] or task["unresolved"]:
            raise WorkflowError("A resolved delivery is required")
        attempt = task["attempts"][-1]
        if not attempt.get("turn_completed") or not attempt.get("delivery"):
            raise WorkflowError("A successful execution and complete delivery are required")
        packet = _reference(attempt["delivery"]["evidence"])
        final = _reference(packet["snapshot"])
        if (packet.get("task_id") != task["task_id"] or packet.get("project") != task["project"]
                or packet.get("attempt_number") != attempt["number"]
                or packet.get("requirements_hash") != attempt["requirements_hash"]
                or packet.get("requirements_version") != attempt["requirements_version"]
                or final.get("snapshot_hash") != attempt["delivery"]["snapshot_hash"]
                or not final.get("complete") or not packet["comparison"].get("complete")):
            raise WorkflowError("Delivery evidence does not match the current attempt")
        return attempt, packet, final

    @staticmethod
    def _tests(directory):
        ledger = directory / "tests.json"
        if not ledger.exists():
            return []
        records = _read(ledger).get("records")
        if not isinstance(records, list):
            raise WorkflowError("Invalid test evidence ledger")
        result = []
        for reference in records:
            record = _reference(reference)
            output = record["output"]
            if evidence_ref(output["path"])["sha256"] != output["sha256"]:
                raise WorkflowError("Saved test output changed")
            result.append(dict(record, record_evidence=reference))
        return result

    def record_test(self, task_id, argv, *, actor, timeout=60):
        timeout = _seconds(timeout, "timeout")
        if (not isinstance(argv, (list, tuple)) or not argv or not argv[0]
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv)):
            raise WorkflowError("Test command must be a nonempty argv list without NUL bytes")
        with self.store.locked({"task_id": task_id, "action": "test", "runner_pid": os.getpid()}):
            task = self.store.read(task_id)
            self._actor(task, actor)
            self._binding(task)
            attempt, _packet, final = self._delivery(task)
            if not git_evidence.verify_snapshot(task["project"], final)["matches"]:
                raise WorkflowError("Worktree changed before tests; refresh delivery first")
            directory = self._directory(task_id) / attempt["directory"]
            identifier = str(uuid.uuid4())
            output_path = directory / ("test-" + identifier + ".log")
            timed_out, launch_error, exit_code = False, None, None
            with output_path.open("xb") as output:
                os.chmod(output_path, 0o600)
                # A header keeps evidence nonempty even for a successful silent command.
                output.write(("$ " + shlex.join(argv) + "\n").encode())
                output.flush()
                try:
                    process = subprocess.Popen(list(argv), cwd=task["project"], stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=subprocess.STDOUT, shell=False,
                                               start_new_session=True)
                    try:
                        exit_code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        exit_code = process.wait()
                    except BaseException:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise
                except OSError as exc:
                    launch_error = str(exc)
                    output.write(("Launch failed: " + launch_error + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            after = git_evidence.verify_snapshot(task["project"], final)
            record = {"id": identifier, "task_id": task_id, "attempt_number": attempt["number"],
                      "snapshot_hash": final["snapshot_hash"], "requirements_hash": attempt["requirements_hash"],
                      "requirements_version": attempt["requirements_version"], "argv": list(argv),
                      "command": shlex.join(argv), "exit_code": exit_code, "timed_out": timed_out,
                      "launch_error": launch_error, "snapshot_verified_after": after["matches"],
                      "output": evidence_ref(output_path), "at": now()}
            record_path = directory / ("test-" + identifier + ".json")
            _write(record_path, record)
            ledger = directory / "tests.json"
            records = _read(ledger)["records"] if ledger.exists() else []
            reference = evidence_ref(record_path)
            _write(ledger, {"records": records + [reference]})
            return dict(record, record_evidence=reference)

    def review(self, task_id, *, actor, decision, reviewed_paths, notes, test_ids=None):
        with self.store.locked({"task_id": task_id, "action": "review", "runner_pid": os.getpid()}):
            task = self.store.read(task_id)
            self._actor(task, actor)
            binding = self._binding(task)
            attempt, packet, final = self._delivery(task)
            if (attempt["requirements_hash"] != task["requirements"]["hash"]
                    or attempt["requirements_version"] != task["requirements"]["version"]):
                raise WorkflowError("Requirements changed; this delivery needs a new execution")
            comparison = git_evidence.compare(_reference(binding["baseline"]), final)
            paths = sorted({path for change in comparison["changes"]
                            for path in (change["before_path"], change["path"])})
            if (not comparison["complete"] or comparison != packet["comparison"]
                    or paths != attempt["delivery"]["changed_paths"]):
                raise WorkflowError("Final review packet is incomplete or changed")
            records = self._tests(self._directory(task_id) / attempt["directory"])
            if test_ids is not None:
                if not isinstance(test_ids, (list, tuple)) or len(set(test_ids)) != len(test_ids):
                    raise WorkflowError("test_ids must be a list of unique recorded IDs")
                selected = [record for record in records if record["id"] in test_ids]
                if {record["id"] for record in selected} != set(test_ids):
                    raise WorkflowError("Review names an unknown test evidence ID")
                records = selected
            else:
                records = [record for record in records if record["snapshot_hash"] == final["snapshot_hash"]]
            tests = []
            for record in records:
                if (record["task_id"] != task_id or record["attempt_number"] != attempt["number"]
                        or record["snapshot_hash"] != final["snapshot_hash"]
                        or record["requirements_hash"] != attempt["requirements_hash"]
                        or record["requirements_version"] != attempt["requirements_version"]):
                    raise WorkflowError("Test evidence belongs to another attempt, snapshot or requirements version")
                if decision == "pass" and (type(record["exit_code"]) is not int or record["exit_code"] != 0
                                           or record["timed_out"] or record["launch_error"]
                                           or not record["snapshot_verified_after"]):
                    raise WorkflowError("Pass requires actual successful tests against the unchanged final snapshot")
                if type(record["exit_code"]) is int:
                    tests.append(dict(record["output"], command=record["command"], exit_code=record["exit_code"],
                                      test_id=record["id"], attempt_number=attempt["number"],
                                      snapshot_hash=final["snapshot_hash"], record_evidence=record["record_evidence"]))
            # Do this last, immediately before the acceptance mutation.
            if not git_evidence.verify_snapshot(task["project"], final)["matches"]:
                raise WorkflowError("Final worktree changed; no review decision was recorded")
            self.store.record_review(task_id, actor=actor, decision=decision, attempt_number=attempt["number"],
                                     requirements_hash=task["requirements"]["hash"], snapshot_hash=final["snapshot_hash"],
                                     reviewed_paths=reviewed_paths, test_evidence=tests, notes=notes)
            return self.status(task_id)

    def stop(self, task_id):
        """Queue exact cancellation without acquiring the monitor's worker lock."""
        task = self.store.read(task_id)
        self._binding(task)
        if not task["unresolved"] or not task["attempts"] or not task["attempts"][-1].get("execution"):
            raise WorkflowError("Stop requires an unresolved execution with its actual bound ID")
        attempt = task["attempts"][-1]
        request = dict(attempt["execution"], task_id=task_id, attempt_number=attempt["number"],
                       client_message_id=attempt["client_message_id"])
        path = self._directory(task_id) / attempt["directory"] / "stop-requested.json"
        if path.exists():
            if _read(path) != request:
                raise WorkflowError("Existing stop request targets another execution")
        else:
            _write(path, request)
        result = self.status(task_id)
        result["workflow"]["stop_pending"] = True
        return result

    def _apply_stop(self, task, adapter):
        attempt = task["attempts"][-1]
        directory = self._directory(task["task_id"]) / attempt["directory"]
        request_path = directory / "stop-requested.json"
        if not request_path.exists():
            return
        identity = attempt.get("execution")
        if not identity:
            raise WorkflowError("Cannot interrupt without a bound execution")
        expected = dict(identity, task_id=task["task_id"], attempt_number=attempt["number"],
                        client_message_id=attempt["client_message_id"])
        if _read(request_path) != expected:
            raise WorkflowError("Stop request does not match the exact current execution")
        sent_path = directory / "stop-sent.json"
        if sent_path.exists():
            if _read(sent_path).get("request") != expected:
                raise WorkflowError("Saved cancellation belongs to another execution")
            return
        _write(sent_path, {"request": expected, "submission_state": "unknown", "at": now()})
        adapter.interrupt(attempt["client_message_id"], identity["execution_id"])
