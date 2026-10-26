"""Linux cancellation of new command processes attributed to one Codex thread.

Exact-turn IPC stops inference but intentionally leaves unified-exec terminals.
Never issue an unscoped second interrupt: it could stop a newer turn. Instead pin
the already identified command processes with pidfds before the native interrupt.
Only current-user processes with Codex's matching thread/session environment, or
their observed children, are eligible. Processes present before dispatch are not.
Environment values other than these two identifiers are never retained.
"""
import os
from pathlib import Path
import select
import signal
import time


class ProcessError(RuntimeError):
    pass


def _process(pid, root=Path("/proc")):
    folder = root / str(pid)
    if folder.stat().st_uid != os.getuid():
        return None
    raw = (folder / "stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    if fields[0] in ("Z", "X"):
        return None
    return {"pid": pid, "start_ticks": int(fields[19]), "parent": int(fields[1])}


def _processes(root=Path("/proc")):
    result = {}
    for folder in root.iterdir():
        if not folder.name.isdigit():
            continue
        try:
            item = _process(int(folder.name), root)
            if item:
                result[item["pid"]] = item
        except (OSError, ValueError, IndexError):
            continue  # Exited or inaccessible; never signal an unidentified PID.
    return result


def _boot(root):
    return (root / "sys/kernel/random/boot_id").read_text().strip()


def baseline(root=Path("/proc")):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise ProcessError("Exact command cancellation requires Linux pidfd support")
    return {"boot_id": _boot(root), "processes": [[p["pid"], p["start_ticks"]]
                                                for p in _processes(root).values()]}


def _matches(pid, session, root):
    try:
        with (root / str(pid) / "environ").open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return False
        values = [item.split(b"=", 1)[1] for item in raw.split(b"\0")
                  if item.startswith((b"CODEX_THREAD_ID=", b"CODEX_SESSION_ID="))]
        return bool(values) and all(value == session.encode("ascii") for value in values)
    except (OSError, UnicodeError):
        return False


def candidates(session, before, root=Path("/proc")):
    if not isinstance(before, dict) or before.get("boot_id") != _boot(root):
        raise ProcessError("Original process baseline is missing or belongs to a different boot")
    old = {tuple(pair) for pair in before["processes"]}
    current = {pid: p for pid, p in _processes(root).items()
               if (pid, p["start_ticks"]) not in old}
    selected = {pid for pid in current if _matches(pid, session, root)}
    while True:
        children = {pid for pid, p in current.items() if p["parent"] in selected}
        if children <= selected:
            break
        selected |= children
    return [current[pid] for pid in sorted(selected)]


class PinnedProcesses:
    """Pin identities before interrupt, close every pidfd even on transport failure."""

    def __init__(self, targets):
        self.targets = targets
        self.handles = []

    def __enter__(self):
        try:
            for target in self.targets:
                try:
                    descriptor = os.pidfd_open(target["pid"], 0)
                except ProcessLookupError:
                    continue
                try:
                    current = _process(target["pid"])
                    if not current or current["start_ticks"] != target["start_ticks"]:
                        os.close(descriptor)
                        continue
                except FileNotFoundError:
                    os.close(descriptor)
                    continue
                except BaseException:
                    os.close(descriptor)
                    raise
                self.handles.append((target, descriptor))
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        for _target, descriptor in self.handles:
            os.close(descriptor)
        self.handles = []

    def terminate(self):
        remaining = list(self.handles)
        for signum, duration in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
            for _target, descriptor in reversed(remaining):
                try:
                    signal.pidfd_send_signal(descriptor, signum)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + duration
            while remaining and time.monotonic() < deadline:
                ready, _, _ = select.select([fd for _p, fd in remaining], [], [], 0.05)
                remaining = [(p, fd) for p, fd in remaining if fd not in ready]
            if not remaining:
                break
        return {"complete": not remaining, "targets": self.targets,
                "remaining": [p for p, _fd in remaining]}
