"""Private, content-addressed Git worktree evidence; no index or ref writes.

Snapshots cover tracked paths, non-ignored untracked paths, and every path in the
supplied baseline. Pass the previous delivery as baseline when capturing a later
delivery so files subsequently hidden by .gitignore remain in scope. Comparison
may still use the original task baseline. Exact-content renames are paired;
ambiguous renames and renames with edits remain explicit delete/add pairs.

This module establishes evidence integrity and coverage, not semantic acceptance
or proof that an agent read it. A review must bind its delivery snapshot_hash and
require verify_snapshot(...)["matches"] immediately before recording passage.
"""
import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import stat
import subprocess
import threading
import time


SCHEMA = 1
KIND = "claudex_git_snapshot"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 10000
MAX_DIFF_BYTES = 8 * 1024 * 1024
MAX_GIT_BYTES = 8 * 1024 * 1024
SCOPE = "tracked_and_unignored_untracked_plus_baseline_paths"


class EvidenceError(ValueError):
    pass


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise EvidenceError(label + " must be a positive integer")
    return value


def _git(project, *arguments, allow_failure=False):
    """Bound subprocess output without running Git pagers, filters or fsmonitor."""
    command = ["git", "--no-optional-locks", "-C", str(project),
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *arguments]
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
        environment.pop(name, None)
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=environment) as process:
        output, errors, pending = bytearray(), bytearray(), queue.Queue(maxsize=8)
        cancelled = threading.Event()

        def consume(stream, channel):
            while not cancelled.is_set():
                raw = stream.read(65536)
                while not cancelled.is_set():
                    try:
                        pending.put((channel, raw), timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not raw:
                    return

        readers = [threading.Thread(target=consume, args=(stream, channel), daemon=True)
                   for stream, channel in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
        for reader in readers:
            reader.start()
        try:
            deadline, remaining_streams = time.monotonic() + 30, 2
            while remaining_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EvidenceError("Git inspection timed out")
                try:
                    channel, raw = pending.get(timeout=min(remaining, 1))
                except queue.Empty:
                    continue
                if not raw:
                    remaining_streams -= 1
                    continue
                destination, limit = (output, MAX_GIT_BYTES) if channel == "stdout" else (errors, 65536)
                if len(destination) + len(raw) > limit:
                    raise EvidenceError("Git inspection output exceeded its size limit")
                destination.extend(raw)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            cancelled.set()
            for reader in readers:
                reader.join(timeout=1)
    if code and not allow_failure:
        raise EvidenceError("Git inspection failed: " + bytes(errors).decode(errors="replace")[:500])
    return code, bytes(output)


def _project(path):
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise EvidenceError("Project directory does not exist")
    _, raw = _git(path, "rev-parse", "--show-toplevel")
    root = Path(os.fsdecode(raw[:-1] if raw.endswith(b"\n") else raw)).resolve()
    if not root.is_dir() or (path != root and root not in path.parents):
        raise EvidenceError("Git did not identify a worktree")
    return root


def _safe_path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise EvidenceError("Invalid snapshot path")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(part in ("", ".", "..", ".git") for part in parts):
        raise EvidenceError("Snapshot path escapes the worktree or names Git internals")
    return value


def _git_state(project):
    code, head = _git(project, "rev-parse", "--verify", "HEAD", allow_failure=True)
    # An unborn branch is valid; other failures must not look like an empty repo.
    if code:
        branch_code, _ = _git(project, "symbolic-ref", "-q", "HEAD", allow_failure=True)
        if branch_code:
            raise EvidenceError("Cannot determine Git HEAD")
        head = b""
    _, index = _git(project, "ls-files", "--stage", "-z")
    _, untracked = _git(project, "ls-files", "--others", "--exclude-standard", "-z")
    _, status_bytes = _git(project, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths, submodules = set(), set()
    for row in index.split(b"\0"):
        if not row:
            continue
        try:
            metadata, name = row.split(b"\t", 1)
            mode, _object_id, _stage = metadata.split()
        except ValueError:
            raise EvidenceError("Unrecognized Git index record") from None
        name = _safe_path(os.fsdecode(name))
        paths.add(name)
        if mode == b"160000":
            submodules.add(name)
    paths.update(_safe_path(os.fsdecode(name[:-1] if name.endswith(b"/") else name))
                 for name in untracked.split(b"\0") if name)
    metadata = {"head": head.decode("ascii").strip() or None,
                "index_sha256": _digest(index), "status_sha256": _digest(status_bytes)}
    return metadata, paths, submodules, {"index.stage": index, "status.porcelain": status_bytes, "head.txt": head}


def _lstat(project, relative):
    path = project
    parts = relative.split("/")
    for component in parts[:-1]:
        path = path / component
        try:
            info = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        # Never follow an ancestor symlink into an unrelated directory. A former
        # child of a replaced directory is absent from this worktree tree.
        if not stat.S_ISDIR(info.st_mode):
            return None
    try:
        return (project / relative).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _identity(info):
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _write(path, raw):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _issue(code, path=None, detail=None):
    result = {"code": code}
    if path is not None:
        result["path"] = path
    if detail is not None:
        result["detail"] = detail
    return result


def _payload(manifest):
    return {key: manifest[key] for key in ("schema_version", "kind", "project", "scope", "git", "entries", "complete", "issues", "limits")}


def _validate(manifest):
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA
            or manifest.get("kind") != KIND or manifest.get("scope") != SCOPE
            or not isinstance(manifest.get("entries"), dict) or type(manifest.get("complete")) is not bool):
        raise EvidenceError("Unsupported snapshot manifest")
    try:
        if _digest(_canonical(_payload(manifest))) != manifest["snapshot_hash"]:
            raise EvidenceError("Snapshot manifest hash changed")
        for relative, entry in manifest["entries"].items():
            _safe_path(relative)
            if entry.get("kind") not in ("file", "symlink", "missing", "unavailable"):
                raise EvidenceError("Unsupported snapshot entry")
            if entry.get("kind") in ("file", "symlink"):
                digest = entry.get("sha256")
                if (not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)
                        or type(entry.get("size")) is not int or entry["size"] < 0):
                    raise EvidenceError("Invalid snapshot content reference")
    except (KeyError, TypeError, AttributeError):
        raise EvidenceError("Malformed snapshot manifest") from None


def _scan(project, limits, baseline=None, destination=None):
    metadata, paths, submodules, git_files = _git_state(project)
    if baseline is not None:
        paths.update(baseline["entries"])
    issues, entries, identities = [], {}, {}
    if len(paths) > limits["max_files"]:
        issues.append(_issue("file_count_limit", detail={"count": len(paths), "limit": limits["max_files"]}))
    used = 0
    for relative in sorted(paths)[:limits["max_files"]]:
        info = None
        try:
            info = _lstat(project, relative)
            identities[relative] = _identity(info)
            if info is None:
                entries[relative] = {"kind": "missing"}
                continue
            if relative in submodules:
                raise EvidenceError("Submodule contents need their own snapshot")
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                raw = os.fsencode(os.readlink(project / relative))
                kind = "symlink"
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > limits["max_file_bytes"]:
                    raise EvidenceError("File exceeds max_file_bytes")
                if used + info.st_size > limits["max_total_bytes"]:
                    raise EvidenceError("Snapshot exceeds max_total_bytes")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(project / relative, flags)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(info):
                        raise EvidenceError("File changed while opening")
                    raw = stream.read(limits["max_file_bytes"] + 1)
                    if _identity(os.fstat(stream.fileno())) != _identity(info):
                        raise EvidenceError("File changed while reading")
                kind = "file"
            else:
                raise EvidenceError("Special file or directory cannot be silently omitted")
            if len(raw) > limits["max_file_bytes"] or used + len(raw) > limits["max_total_bytes"]:
                raise EvidenceError("Content exceeds snapshot size limits")
            if _identity(_lstat(project, relative)) != _identity(info):
                raise EvidenceError("Path changed during capture")
            used += len(raw)
            digest = _digest(raw)
            entries[relative] = {"kind": kind, "mode": mode, "size": len(raw), "sha256": digest}
            if destination is not None:
                target = destination / "blobs" / digest
                if not target.exists():
                    _write(target, raw)
        except (OSError, EvidenceError) as exc:
            entries[relative] = {"kind": "unavailable", "size": info.st_size if info is not None else None}
            issues.append(_issue("content_unavailable", relative, str(exc)))
    # Detect additions, index/ref changes and files modified after they were read.
    after_metadata, after_paths, _, _ = _git_state(project)
    if baseline is not None:
        after_paths.update(baseline["entries"])
    if metadata != after_metadata or paths != after_paths:
        issues.append(_issue("git_changed_during_capture"))
    for relative, identity in identities.items():
        try:
            if _identity(_lstat(project, relative)) != identity:
                issues.append(_issue("path_changed_during_capture", relative))
        except OSError as exc:
            issues.append(_issue("path_changed_during_capture", relative, str(exc)))
    result = {"schema_version": SCHEMA, "kind": KIND, "project": str(project), "scope": SCOPE,
              "git": metadata, "entries": entries, "complete": not issues, "issues": issues,
              "limits": limits, "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "evidence_dir": str(destination) if destination is not None else None}
    result["snapshot_hash"] = _digest(_canonical(_payload(result)))
    if destination is not None:
        for name, raw in git_files.items():
            _write(destination / "git" / name, raw)
    return result


def capture(project, evidence_dir, *, baseline=None, max_file_bytes=MAX_FILE_BYTES,
            max_total_bytes=MAX_TOTAL_BYTES, max_files=MAX_FILES):
    """Create a new private evidence directory, without changing the Git project.

    Incomplete evidence is returned and saved with complete=False. It must never
    qualify for acceptance. Existing output directories are never overwritten.
    """
    project = _project(project)
    destination = Path(evidence_dir).expanduser().resolve()
    if destination == project or project in destination.parents:
        raise EvidenceError("Evidence directory must be outside the project")
    if destination.exists() or not destination.parent.is_dir():
        raise EvidenceError("Choose a new evidence directory with an existing parent")
    limits = {"max_file_bytes": _positive(max_file_bytes, "max_file_bytes"),
              "max_total_bytes": _positive(max_total_bytes, "max_total_bytes"),
              "max_files": _positive(max_files, "max_files")}
    if baseline is not None:
        _validate(baseline)
        if baseline["project"] != str(project):
            raise EvidenceError("Baseline belongs to another project")
    destination.mkdir(mode=0o700)
    (destination / "blobs").mkdir(mode=0o700)
    (destination / "git").mkdir(mode=0o700)
    result = _scan(project, limits, baseline, destination)
    _write(destination / "manifest.json", json.dumps(result, ensure_ascii=True, indent=2).encode() + b"\n")
    return result


def _blob(manifest, entry):
    directory = Path(manifest["evidence_dir"])
    folder = directory / "blobs"
    path = folder / entry["sha256"]
    if directory.is_symlink() or folder.is_symlink() or path.is_symlink():
        raise EvidenceError("Evidence content must not be a symlink")
    if not path.is_file() or path.stat().st_size != entry["size"]:
        raise EvidenceError("Evidence content is missing or has changed size")
    with path.open("rb") as stream:
        raw = stream.read(entry["size"] + 1)
    if len(raw) != entry["size"] or _digest(raw) != entry["sha256"]:
        raise EvidenceError("Evidence content hash changed")
    return raw


def _check_blobs(manifest):
    issues = []
    for relative, entry in manifest["entries"].items():
        if entry["kind"] in ("file", "symlink"):
            try:
                _blob(manifest, entry)
            except (OSError, EvidenceError, TypeError) as exc:
                issues.append(_issue("evidence_content_invalid", relative, str(exc)))
    try:
        directory = Path(manifest["evidence_dir"])
        folder = directory / "git"
        if directory.is_symlink() or folder.is_symlink():
            raise EvidenceError("Git evidence must not be a symlink")
        for name, expected in (("index.stage", manifest["git"]["index_sha256"]),
                               ("status.porcelain", manifest["git"]["status_sha256"]),
                               ("head.txt", None)):
            path = folder / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_GIT_BYTES:
                raise EvidenceError("Git evidence is missing, unsafe or oversized")
            raw = path.read_bytes()
            if expected is not None:
                valid = _digest(raw) == expected
            else:
                valid = (raw.decode("ascii").strip() or None) == manifest["git"]["head"]
            if not valid:
                raise EvidenceError("Git evidence content changed")
    except (OSError, EvidenceError, TypeError, UnicodeDecodeError) as exc:
        issues.append(_issue("git_evidence_invalid", detail=str(exc)))
    return issues


def _present(entry):
    return entry is not None and entry["kind"] != "missing"


def _reference(manifest, relative):
    entry = manifest["entries"].get(relative)
    if entry is None:
        return {"kind": "missing"}
    result = dict(entry)
    if entry["kind"] in ("file", "symlink"):
        result["content_file"] = str(Path(manifest["evidence_dir"]) / "blobs" / entry["sha256"])
    return result


def _text(raw):
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def compare(before, after, *, max_diff_bytes=MAX_DIFF_BYTES):
    """Return actual net changes and reviewable content, independent of commits."""
    _positive(max_diff_bytes, "max_diff_bytes")
    _validate(before)
    _validate(after)
    if before["project"] != after["project"]:
        raise EvidenceError("Cannot compare different projects")
    issues = []
    for label, manifest in (("baseline", before), ("delivery", after)):
        if not manifest["complete"]:
            issues.append(_issue(label + "_incomplete", detail=manifest["issues"]))
        issues.extend(_check_blobs(manifest))
    omitted = set(before["entries"]) - set(after["entries"])
    if omitted:
        issues.append(_issue("baseline_paths_not_observed", detail=sorted(omitted)))
    old, new = before["entries"], after["entries"]
    removed = [path for path in old if _present(old[path]) and not _present(new.get(path))]
    added = [path for path in new if _present(new[path]) and not _present(old.get(path))]
    renames, removed_by_content, added_by_content = {}, {}, {}
    for names, entries, grouped in ((removed, old, removed_by_content), (added, new, added_by_content)):
        for path in names:
            entry = entries[path]
            if entry["kind"] in ("file", "symlink"):
                grouped.setdefault((entry["kind"], entry["sha256"]), []).append(path)
    for key, sources in removed_by_content.items():
        targets = added_by_content.get(key, [])
        if len(sources) == len(targets) == 1:
            renames[sources[0]] = targets[0]
    renamed_targets = set(renames.values())
    changes, diff_used = [], 0
    candidates = [(source, target, "renamed") for source, target in sorted(renames.items())]
    for path in sorted(set(old) | set(new)):
        if path in renames or path in renamed_targets or old.get(path, {"kind": "missing"}) == new.get(path, {"kind": "missing"}):
            continue
        operation = "added" if not _present(old.get(path)) else "deleted" if not _present(new.get(path)) else "modified"
        candidates.append((path, path, operation))
    for old_path, new_path, operation in candidates:
        left, right = _reference(before, old_path), _reference(after, new_path)
        change = {"change": operation, "path": new_path, "before_path": old_path,
                  "before": left, "after": right, "diff": None, "diff_complete": True}
        try:
            if left["kind"] == "unavailable" or right["kind"] == "unavailable":
                raise EvidenceError("Content was not completely captured")
            left_raw = _blob(before, left) if _present(left) else b""
            right_raw = _blob(after, right) if _present(right) else b""
            left_text, right_text = _text(left_raw), _text(right_raw)
            change["binary"] = left_text is None or right_text is None
            if not change["binary"]:
                pieces = []
                # splitlines(keepends=True) preserves final-newline changes in
                # content hashes; repr flags make this edge explicit in reports.
                change["before_ends_with_newline"] = left_raw.endswith(b"\n")
                change["after_ends_with_newline"] = right_raw.endswith(b"\n")
                for part in difflib.unified_diff(left_text.splitlines(keepends=True), right_text.splitlines(keepends=True),
                                                fromfile="a/" + json.dumps(old_path, ensure_ascii=True),
                                                tofile="b/" + json.dumps(new_path, ensure_ascii=True)):
                    if not part.endswith("\n"):
                        part += "\n\\ No newline at end of file\n"
                    size = len(part.encode("utf-8"))
                    if diff_used + size > max_diff_bytes:
                        raise EvidenceError("Review diff exceeds max_diff_bytes")
                    diff_used += size
                    pieces.append(part)
                change["diff"] = "".join(pieces)
        except (OSError, EvidenceError, TypeError) as exc:
            change["diff_complete"] = False
            issues.append(_issue("diff_unavailable", new_path, str(exc)))
        changes.append(change)
    return {"schema_version": SCHEMA, "kind": "claudex_git_comparison", "project": before["project"],
            "baseline_snapshot_hash": before["snapshot_hash"], "delivery_snapshot_hash": after["snapshot_hash"],
            "complete": not issues, "issues": issues, "git_metadata_changed": before["git"] != after["git"],
            "before_git": before["git"], "after_git": after["git"], "changes": changes}


def verify_snapshot(project, manifest):
    """Read-only verification against live files and Git metadata; no new files."""
    _validate(manifest)
    issues = _check_blobs(manifest)
    if not manifest["complete"]:
        issues.append(_issue("snapshot_incomplete", detail=manifest["issues"]))
    try:
        project = _project(project)
        if str(project) != manifest["project"]:
            raise EvidenceError("Snapshot belongs to another project")
        limits = {key: _positive(manifest["limits"][key], key)
                  for key in ("max_file_bytes", "max_total_bytes", "max_files")}
        actual = _scan(project, limits, baseline=manifest)
    except (EvidenceError, OSError, KeyError) as exc:
        return {"matches": False, "complete": False, "snapshot_hash": manifest["snapshot_hash"],
                "current_snapshot_hash": None, "issues": issues + [_issue("verification_failed", detail=str(exc))]}
    issues.extend(actual["issues"])
    return {"matches": not issues and actual["snapshot_hash"] == manifest["snapshot_hash"],
            "complete": not issues, "snapshot_hash": manifest["snapshot_hash"],
            "current_snapshot_hash": actual["snapshot_hash"], "issues": issues}
