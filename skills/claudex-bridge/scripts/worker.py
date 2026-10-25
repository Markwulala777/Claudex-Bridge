#!/usr/bin/env python3
"""Claude coordinates one visible, desktop-owned Codex conversation. Stdlib only."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from desktop_bridge import BridgeError, Desktop, create_thread, turns, validate_rollout, verify_installation


CONTRACT = """你是当前任务的主 Codex 执行者，Claude 是需求负责人和验收者；同一任务不交换主角色。
遵守项目 AGENTS.md，保留已有改动。只完成下面任务及其必要测试。
允许你按需启动 Codex 子代理处理真正独立的子任务；给出明确输入、输出与完成标准。
由你汇总、验证子代理结果并负责最终交付；不要为简单任务或连续决策增加子代理。
不要启动另一个主 Codex、反向委派给 Claude，或递归调用 codex-worker/claudex-bridge。
你和子代理都不能调用 start、resume 或 review 来派工或批准自己的交付。
不要自行扩大任务为发布、推送、部署或修改供应商/全局配置。
发现需求歧义、权限/额度问题或无法推进的阻碍时，如实说明，不声称完成。
结束时简明报告：实际改动、执行的测试命令和结果、未完成事项。
你的执行结束不代表 Claude 已验收通过。

--- Claude 的任务或返工要求 ---
"""
BACKEND = "desktop-ipc-v2"
TERMINAL = {"completed", "failed", "interrupted"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def state_root():
    default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    root = Path(os.environ.get("CODEX_WORKER_STATE_DIR", str(default / "claude-codex-worker"))).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def task_dir(root, task_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task_id):
        raise ValueError("task ID must be 1–80 letters, digits, underscores or hyphens")
    return root / task_id


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


@contextlib.contextmanager
def worker_lock(root, owner=None):
    with (root / "worker.lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            raise RuntimeError("A runner is already monitoring a task; do not start another. Owner: " + lock.read()[:500])
        if owner is not None:
            lock.seek(0)
            lock.truncate()
            json.dump(owner, lock)
            lock.flush()
        yield


def active_owner(root):
    with (root / "worker.lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None
        except BlockingIOError:
            lock.seek(0)
            try:
                return json.load(lock)
            except ValueError:
                return {"task_id": "(unknown)"}


def summary(root, directory, data):
    owner = active_owner(root)
    active = bool(owner and owner.get("task_id") == data["task_id"])
    status = data["status"]
    if data.get("unresolved") and not active and status in ("running", "submitting"):
        status = "detached"
    result = {
        "task_id": data["task_id"], "project": data["project"],
        "backend": data.get("backend", "legacy-cli"), "thread_id": data.get("thread_id"),
        "desktop_url": f"codex://threads/{data['thread_id']}" if data.get("thread_id") else None,
        "status": status, "monitor_running": active,
        "execution_unresolved": data.get("unresolved", False),
        "task_directory": str(directory), "attempt_count": len(data.get("attempts", [])),
        "message": "Only needs_review means execution was verified. Claude must still review actual changes and test evidence.",
    }
    if data.get("unresolved"):
        result["message"] = "Desktop work may still be active. Use watch to reconnect; never start a replacement or blindly resume."
    if data.get("attempts"):
        last = data["attempts"][-1]
        folder = directory / last["directory"]
        result["latest_attempt"] = {
            "number": last["number"], "turn_id": last.get("turn_id"),
            "turn_completed": last.get("turn_completed", False),
            "submission_state": last.get("submission_state"),
            "result_file": str(folder / "result.txt"), "events_file": str(folder / "events.jsonl"),
            "desktop_events_file": str(folder / "desktop-events.jsonl"),
        }
        if last.get("error"):
            result["latest_attempt"]["error"] = last["error"]
    if data.get("review"):
        result["review"] = data["review"]
    return result


def prompt_text(path):
    value = Path(path).expanduser().resolve().read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("prompt/notes file is empty")
    return value


def git_project(path):
    project = Path(path).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("project directory does not exist")
    result = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("A Git project is required")
    return str(Path(result.stdout.strip()).resolve())


def assert_no_unresolved(root):
    # Desktop turns outlive this process and its flock. Persistent state is also
    # required: losing a monitor must not permit a second worker to be submitted.
    for metadata in root.glob("*/task.json"):
        other = read_json(metadata)
        if other.get("unresolved") or other.get("status") == "running":
            raise RuntimeError(f"Task {other['task_id']} has an unresolved execution. Reconnect with watch before submitting any more work.")


def find_turn(state, attempt):
    available = turns(state)
    if attempt.get("turn_id"):
        return next((turn for turn in available if turn["turnId"] == attempt["turn_id"]), None)
    matches = [turn for turn in available if turn.get("params", {}).get("clientUserMessageId") == attempt["client_message_id"]]
    if len(matches) == 1:
        attempt["turn_id"] = matches[0]["turnId"]
        attempt["submission_state"] = "started"
        return matches[0]
    return None


class RolloutTail:
    def __init__(self, data, attempt, folder):
        self.path = validate_rollout(data["rollout_path"])
        self.attempt = attempt
        self.events = folder / "events.jsonl"
        self.completions = {}
        if self.events.exists():
            with self.events.open("rb") as stream:
                for line in stream:
                    self.observe(line)

    def observe(self, raw):
        try:
            event = json.loads(raw)
        except ValueError:
            return
        payload = event.get("payload", {})
        if event.get("type") == "event_msg" and payload.get("type") == "task_complete":
            self.completions[payload.get("turn_id")] = payload

    def poll(self):
        position = self.attempt.get("log_offset", self.attempt["rollout_start"])
        with self.path.open("rb") as source, self.events.open("ab") as target:
            if source.seek(0, os.SEEK_END) < position:
                raise BridgeError("Rollout shrank; inspect it manually before continuing")
            source.seek(position)
            while True:
                start = source.tell()
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    self.attempt["log_offset"] = start
                    break
                target.write(line)
                self.observe(line)
            target.flush()


def monitor(desktop, data, attempt, directory):
    folder = directory / attempt["directory"]
    metadata = directory / "task.json"
    tail = RolloutTail(data, attempt, folder)
    first = True
    missing_since = None
    completed_since = None
    next_snapshot = 0
    while True:
        if first or time.monotonic() >= next_snapshot:
            state = desktop.snapshot()
            turn = find_turn(state, attempt)
            next_snapshot = time.monotonic() + 10
            first = False
            if turn is None:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since > 30:
                    raise BridgeError("Cannot identify the original turn in desktop history. Submission remains unresolved; do not resend.")
            else:
                missing_since = None
                status = turn.get("status")
                attempt["desktop_turn_status"] = status
                if status not in TERMINAL and status != "inProgress":
                    raise BridgeError(f"Unknown desktop turn status: {status!r}; no completion was inferred")
                if status in TERMINAL:
                    tail.poll()
                    completion = tail.completions.get(attempt["turn_id"])
                    result = (completion or {}).get("last_agent_message")
                    if status == "completed" and not (isinstance(result, str) and result.strip()):
                        completed_since = completed_since or time.monotonic()
                        if time.monotonic() - completed_since < 30:
                            save(metadata, data)
                            continue
                    attempt["submission_state"] = "terminal"
                    attempt["ended_at"] = now()
                    data["unresolved"] = False
                    if status == "completed" and isinstance(result, str) and result.strip():
                        (folder / "result.txt").write_text(result, encoding="utf-8")
                        attempt["turn_completed"] = True
                        attempt.pop("error", None)
                        data["status"] = "needs_review"
                    else:
                        data["status"] = "interrupted" if status == "interrupted" else "failed"
                        attempt["error"] = str(turn.get("error") or "No verified final result for this exact turn; inspect desktop and raw events.")
                    save(metadata, data)
                    return
                pending = [request for request in state.get("requests", [])
                           if request.get("params", {}).get("turnId") in (None, attempt["turn_id"])]
                if pending and not (folder / "stop-requested.json").exists():
                    data["status"] = "needs_attention"
                    attempt["error"] = "Desktop is awaiting input or approval. Resolve it in that conversation, then run watch."
                    save(metadata, data)
                    return
            save(metadata, data)
        if (folder / "stop-requested.json").exists() and not attempt.get("stop_sent_at"):
            if attempt.get("turn_id"):
                desktop.interrupt(attempt["turn_id"])
                attempt["stop_sent_at"] = now()
                save(metadata, data)
                next_snapshot = 0
        tail.poll()
        desktop.pump(timeout=1)


def execute(args, root):
    directory = task_dir(root, args.task)
    metadata = directory / "task.json"
    project = git_project(args.project) if args.action == "start" else None
    if args.action == "start" and metadata.exists():
        data = read_json(metadata)
        if data["project"] != project:
            raise ValueError("This task ID belongs to a different project")
        emit(summary(root, directory, data))
        return 0
    with worker_lock(root, {"task_id": args.task, "runner_pid": os.getpid()}):
        if args.action == "start" and metadata.exists():
            data = read_json(metadata)
            if data["project"] != project:
                raise ValueError("This task ID belongs to a different project")
            emit(summary(root, directory, data))
            return 0
        if args.action in ("start", "resume"):
            assert_no_unresolved(root)
            prompt = prompt_text(args.prompt_file)
        if args.action == "start":
            directory.mkdir(mode=0o700)
            data = {"task_id": args.task, "project": project, "created_at": now(), "backend": BACKEND,
                    "thread_id": None, "status": "creating", "unresolved": False, "attempts": [],
                    "model": args.model, "effort": args.effort}
            for filename, command in [
                ("baseline-status.txt", ["git", "status", "--porcelain=v1"]),
                ("baseline.patch", ["git", "diff", "--binary", "HEAD"]),
                ("baseline-head.txt", ["git", "rev-parse", "HEAD"]),
            ]:
                snapshot = subprocess.run(command, cwd=project, capture_output=True)
                (directory / filename).write_bytes(snapshot.stdout)
                if snapshot.returncode:
                    (directory / (filename + ".error")).write_bytes(snapshot.stderr)
        else:
            data = read_json(metadata)
            if data.get("backend") != BACKEND:
                raise ValueError("This is a legacy CLI task, not a desktop-owned conversation. It is not silently converted or replaced.")
            if not data.get("thread_id") or not Path(data["project"]).is_dir():
                raise ValueError("No saved thread ID or project directory; inspect the existing task before proceeding")
            if args.action == "resume" and data["status"] == "needs_review":
                raise ValueError("Claude must review the completed attempt before sending a new one")
            if args.action == "watch" and not data.get("unresolved"):
                emit(summary(root, directory, data))
                return 0
        if args.action in ("start", "resume"):
            number = len(data["attempts"]) + 1
            folder = directory / f"attempt-{number:03d}"
            folder.mkdir(mode=0o700)
            (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
            (folder / "worker-input.txt").write_text(CONTRACT + prompt, encoding="utf-8")
            attempt = {"number": number, "directory": folder.name, "kind": args.action,
                       "started_at": now(), "turn_completed": False, "submission_state": "not_sent",
                       "client_message_id": str(uuid.uuid4()), "turn_id": None}
            data["attempts"].append(attempt)
            data.pop("review", None)
        else:
            attempt = data["attempts"][-1]
            folder = directory / attempt["directory"]
        save(metadata, data)
        try:
            binary = verify_installation()
            with (folder / "desktop-events.jsonl").open("a", encoding="utf-8") as log, Desktop(log) as desktop:
                if args.action == "start":
                    def remember(thread_id, rollout):
                        data.update(thread_id=thread_id, rollout_path=rollout)
                        save(metadata, data)
                    create_thread(binary, data["project"], "Claude · " + args.task, directory, remember,
                                  model=data.get("model"), effort=data.get("effort"))
                state = desktop.attach(data["thread_id"])
                if args.action in ("start", "resume"):
                    if (folder / "stop-requested.json").exists():
                        data["status"] = "interrupted"
                        attempt["error"] = "Stopped before submission; no coding turn was started"
                        attempt["ended_at"] = now()
                    else:
                        if any(turn.get("status") == "inProgress" for turn in turns(state)) or state.get("requests"):
                            raise BridgeError("This conversation already has active work or pending input; no new turn was submitted")
                        attempt["rollout_start"] = validate_rollout(data["rollout_path"]).stat().st_size
                        # Persist the ambiguity BEFORE the only mutating send. An
                        # IPC timeout never authorizes another submission.
                        attempt["submission_state"] = "unknown"
                        data.update(status="submitting", unresolved=True)
                        save(metadata, data)
                        attempt["turn_id"] = desktop.start(CONTRACT + prompt, attempt["client_message_id"])
                        attempt["submission_state"] = "started"
                        data["status"] = "running"
                        save(metadata, data)
                        monitor(desktop, data, attempt, directory)
                else:
                    data["status"] = "running"
                    save(metadata, data)
                    monitor(desktop, data, attempt, directory)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            data["status"] = "detached" if data.get("unresolved") else "failed"
            attempt["error"] = str(exc) or "Local monitor was interrupted; the desktop task was not assumed to have stopped"
            attempt["monitor_stopped_at"] = now()
        finally:
            save(metadata, data)
    emit(summary(root, directory, data))
    return 0 if data["status"] == "needs_review" else 1


def record_review(args, root):
    directory = task_dir(root, args.task)
    with worker_lock(root):
        data = read_json(directory / "task.json")
        if args.decision in ("pass", "revise"):
            if data.get("unresolved") or data["status"] not in ("needs_review", "revision_requested", "accepted"):
                raise ValueError("An unresolved/failed execution cannot receive pass or revise")
        notes = prompt_text(args.notes_file)
        number = len(data["attempts"])
        target = directory / f"review-{number:03d}-{uuid.uuid4().hex[:8]}.md"
        target.write_text(notes, encoding="utf-8")
        data["review"] = {"decision": args.decision, "attempt": number, "at": now(), "notes_file": str(target)}
        data["status"] = {"pass": "accepted", "revise": "revision_requested", "blocked": "blocked"}[args.decision]
        save(directory / "task.json", data)
    emit(summary(root, directory, data))
    return 0


def stop(args, root):
    directory = task_dir(root, args.task)
    data = read_json(directory / "task.json")
    if data.get("backend") != BACKEND:
        raise ValueError("This is a legacy CLI task. No desktop conversation or unrelated process was stopped.")
    if not data.get("attempts"):
        emit({"task_id": args.task, "message": "No attempt has been created"})
        return 0
    owner = active_owner(root)
    if not data.get("unresolved") and not (owner and owner.get("task_id") == args.task):
        emit({"task_id": args.task, "message": "No unresolved execution; nothing stopped"})
        return 0
    attempt = data["attempts"][-1]
    save(directory / attempt["directory"] / "stop-requested.json", {"requested_at": now()})
    if owner:
        if owner.get("task_id") != args.task:
            raise RuntimeError("Another task holds the monitor lock. The stop request was saved for this task; no other task was interrupted.")
        emit({"task_id": args.task, "status": "stop_requested", "message": "The monitor will interrupt this exact turn. Check status; a request is not confirmation."})
        return 0
    # A detached worker can only be stopped by reconnecting to its saved desktop
    # turn; no process-name kill, no --last, no guessed thread or turn ID.
    args.action = "watch"
    return execute(args, root)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "resume"):
        command = sub.add_parser(action)
        command.add_argument("task")
        if action == "start":
            command.add_argument("--project", required=True)
            command.add_argument("--model", help="Codex model for the new thread, e.g. gpt-6.1-sol")
            command.add_argument("--effort", help="Reasoning effort for the new thread, e.g. xhigh")
        command.add_argument("--prompt-file", required=True)
    for action in ("status", "stop", "watch"):
        sub.add_parser(action).add_argument("task")
    sub.add_parser("list").add_argument("--project")
    command = sub.add_parser("review")
    command.add_argument("task")
    command.add_argument("--decision", choices=("pass", "revise", "blocked"), required=True)
    command.add_argument("--notes-file", required=True)
    args = parser.parse_args()
    root = state_root()

    def detached(_signum, _frame):
        raise KeyboardInterrupt("Monitor terminated; desktop execution may continue. Recover using watch.")

    signal.signal(signal.SIGTERM, detached)
    try:
        if args.action in ("start", "resume", "watch"):
            return execute(args, root)
        if args.action == "review":
            return record_review(args, root)
        if args.action == "stop":
            return stop(args, root)
        if args.action == "status":
            directory = task_dir(root, args.task)
            emit(summary(root, directory, read_json(directory / "task.json")))
            return 0
        project = git_project(args.project) if args.project else None
        records = [read_json(path) for path in root.glob("*/task.json")]
        records = [item for item in records if project is None or item["project"] == project]
        records.sort(key=lambda item: item["created_at"], reverse=True)
        emit([summary(root, root / item["task_id"], item) for item in records[:20]])
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, KeyboardInterrupt) as exc:
        emit({"error": str(exc), "action": args.action, "task_id": getattr(args, "task", None)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
