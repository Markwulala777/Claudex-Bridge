#!/usr/bin/env python3
"""Install an accepted, self-contained skill; never copy accounts or task state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid

SOURCE = Path(__file__).resolve().parent / "skills" / "claudex-bridge"
MANIFEST = ".claudex-install.json"


class InstallError(RuntimeError):
    pass


def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise InstallError("Refusing a symlink in the skill: " + str(relative))
        if path.is_file() and str(relative) != MANIFEST:
            result[relative.as_posix()] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                           "mode": stat.S_IMODE(path.stat().st_mode)}
        elif not path.is_dir() and str(relative) != MANIFEST:
            raise InstallError("Unsupported skill entry: " + str(relative))
    return result


def destinations(target, home=None, env=None):
    home = Path.home() if home is None else Path(home)
    env = os.environ if env is None else env
    roots = {"claude": Path(env.get("CLAUDE_CONFIG_DIR", str(home / ".claude"))),
             "codex": Path(env.get("CODEX_HOME", str(home / ".codex")))}
    names = ("claude", "codex") if target == "both" else (target,)
    paths = [roots[name].expanduser().absolute() / "skills" / "claudex-bridge" for name in names]
    return list(dict.fromkeys(paths))


def owned(path):
    if path.is_symlink():
        raise InstallError("Refusing a linked destination: " + str(path))
    if not path.exists():
        return False
    if (path / MANIFEST).is_symlink():
        raise InstallError("Refusing a linked installation manifest: " + str(path))
    try:
        saved = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError("Existing directory is not a managed installation: " + str(path)) from exc
    if (not isinstance(saved, dict) or saved.get("kind") != "claudex-bridge-install" or saved.get("schema") != 1
            or saved.get("files") != inventory(path)):
        raise InstallError("Installed files changed; preserve them before updating/removing: " + str(path))
    return True


def install(source, paths, readiness):
    if readiness.get("runtime_ready") is not True:
        raise InstallError("Local native acceptance is not complete; installation remains blocked")
    source = Path(source)
    files = inventory(source)
    if not {"SKILL.md", "scripts/bridge.py", "scripts/workflow.py"}.issubset(files):
        raise InstallError("Skill package is incomplete")
    # Validate every destination before changing either product's installation.
    for path in paths:
        owned(path)
    staged, backups, installed = {}, {}, []
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".claudex-stage-", dir=path.parent))
            staged[path] = stage
            for name in files:
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, destination)
            if inventory(stage) != files:
                raise InstallError("Package changed while copying; nothing was activated")
            (stage / MANIFEST).write_text(json.dumps({"schema": 1, "kind": "claudex-bridge-install",
                "files": files}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        for path in paths:
            owned(path)  # Detect edits during preparation before replacing a copy.
            if path.exists():
                backup = path.with_name(".claudex-backup-" + uuid.uuid4().hex)
                path.rename(backup)
                backups[path] = backup
            staged[path].rename(path)
            installed.append(path)
    except BaseException:
        for path in reversed(installed):
            shutil.rmtree(path)
        for path, backup in backups.items():
            backup.rename(path)
        raise
    finally:
        for stage in staged.values():
            if stage.exists():
                shutil.rmtree(stage)
    for backup in backups.values():
        shutil.rmtree(backup)
    return {"installed": [str(path) for path in paths], "files_per_copy": len(files)}


def uninstall(paths):
    # No runtime gate on removal. Never remove state, receipts or codex-worker.
    existing = [path for path in paths if owned(path)]
    for path in existing:
        shutil.rmtree(path)
    return {"removed": [str(path) for path in existing]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "install", "uninstall"))
    parser.add_argument("--target", choices=("claude", "codex", "both"), default="both")
    args = parser.parse_args()
    paths = destinations(args.target)
    if args.action == "uninstall":
        result = uninstall(paths)
    else:
        sys.path.insert(0, str(SOURCE / "scripts"))
        from native_probe import report
        readiness = report()
        if args.action == "check":
            result = {"destinations": [str(p) for p in paths], "runtime_ready": readiness["runtime_ready"],
                      "release_ready": readiness["release_ready"], "blockers": readiness["runtime_blockers"],
                      "existing_managed": [owned(p) for p in paths]}
        else:
            result = install(SOURCE, paths, readiness)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result.get("runtime_ready") is False else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (InstallError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
