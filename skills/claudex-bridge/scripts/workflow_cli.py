"""CLI wiring for native workflow state. Importing this module is portable."""
import json
from pathlib import Path
import sys


def register(commands):
    create = commands.add_parser("task-create", help="Save a brief, fixed roles and baseline; no model call")
    create.add_argument("--task", required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--initiator", choices=("claude", "codex"), required=True)
    create.add_argument("--initiator-session", required=True)
    create.add_argument("--executor-session", required=True)
    create.add_argument("--requirements-file", required=True)
    create.add_argument("--transport-file", required=True)
    for name in ("dispatch", "watch", "status", "feedback", "review", "test", "stop", "snapshot"):
        part = commands.add_parser("task-" + name)
        part.add_argument("--task", required=True)
        if name in ("dispatch", "feedback", "review", "test", "snapshot"):
            part.add_argument("--initiator", choices=("claude", "codex"), required=True)
            part.add_argument("--initiator-session", required=True)
        if name in ("dispatch", "watch"):
            part.add_argument("--wait", type=float, default=45,
                              help="Wait at most 50 seconds, then reconnect with task-watch")
        if name == "feedback":
            part.add_argument("--requirements-file", required=True)
        if name == "review":
            part.add_argument("--decision", choices=("pass", "revise", "blocked"), required=True)
            part.add_argument("--reviewed-path", action="append", default=[])
            part.add_argument("--notes-file", required=True)
            part.add_argument("--test-id", action="append")
        if name == "test":
            part.add_argument("--timeout", type=float, default=45)
            part.add_argument("argv", nargs="+", help="Exact test executable and arguments (after --)")
    owner = commands.add_parser("prepare-claude-owner", help="Prepare task-local native hooks without starting Claude")
    owner.add_argument("--directory", required=True)
    owner.add_argument("--session-id", required=True)


def read_text(path):
    raw = Path(path).read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("Input file exceeds 1 MiB")
    value = raw.decode("utf-8")
    if not value.strip():
        raise ValueError("Input file is empty")
    return value


def run(args):
    if sys.platform != "linux":
        raise RuntimeError("The native workflow is currently implemented only for Linux")
    if args.command == "prepare-claude-owner":
        from claude_owner import prepare_plugin
        return prepare_plugin(args.directory, args.session_id)
    if args.command == "task-dispatch":
        from native_probe import report
        readiness = report()
        if not readiness["runtime_ready"]:
            return {"execution_started": False, "status": "blocked", "runtime_ready": False,
                    "runtime_blockers": readiness["runtime_blockers"]}
    from execution_adapters import factory
    from workflow import Workflow
    workflow = Workflow(adapter_factory=factory)
    actor = ({"product": args.initiator, "session_id": args.initiator_session}
             if hasattr(args, "initiator") else None)
    if args.command == "task-create":
        return workflow.create(args.task, args.project, actor,
                               {"product": "codex" if args.initiator == "claude" else "claude",
                                "session_id": args.executor_session},
                               read_text(args.requirements_file), json.loads(read_text(args.transport_file)))
    if args.command in ("task-dispatch", "task-watch"):
        if not 0 <= args.wait <= 50:
            raise ValueError("--wait must be between 0 and 50 seconds; use task-watch to continue")
        if args.command == "task-dispatch":
            return workflow.dispatch(args.task, actor=actor, wait_seconds=args.wait)
        return workflow.watch(args.task, wait_seconds=args.wait)
    if args.command == "task-feedback":
        return workflow.feedback(args.task, read_text(args.requirements_file), actor=actor)
    if args.command == "task-test":
        if not 0 < args.timeout <= 50:
            raise ValueError("--timeout must be between 0 and 50 seconds")
        return workflow.record_test(args.task, args.argv, actor=actor, timeout=args.timeout)
    if args.command == "task-review":
        return workflow.review(args.task, actor=actor, decision=args.decision,
                               reviewed_paths=args.reviewed_path, notes=read_text(args.notes_file),
                               test_ids=args.test_id)
    if args.command == "task-stop":
        return workflow.stop(args.task)
    if args.command == "task-snapshot":
        return workflow.refresh_delivery(args.task, actor=actor)
    return workflow.status(args.task)
