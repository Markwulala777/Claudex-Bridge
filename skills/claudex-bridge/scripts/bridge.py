#!/usr/bin/env python3
"""Claudex Bridge development CLI. Diagnostic and native-view helpers only."""
import argparse
import json
import sys

# Doctor should not leave bytecode files in an inspected/installable skill.
sys.dont_write_bytecode = True
from native_probe import report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Read local compatibility evidence without starting models")
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable findings")
    prepare = commands.add_parser("prepare-claude-view", help="Prepare an isolated official VS Code follower profile")
    prepare.add_argument("--directory", required=True, help="New directory for the isolated view")
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--binding", required=True, help="Explicit follower binding JSON")
    prepare.add_argument("--extension", required=True, help="Supported installed official Claude Code extension directory")
    prepare.add_argument("--code", help="VS Code CLI executable")
    open_view = commands.add_parser("open-claude-view", help="Open an already prepared native follower profile")
    open_view.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    if args.command != "doctor":
        import native_view
        try:
            if args.command == "prepare-claude-view":
                result = native_view.prepare(args.directory, args.project, args.binding, args.extension, args.code)
            else:
                result = native_view.open_view(args.directory)
        except (native_view.ViewError, OSError, ValueError, KeyError) as exc:
            print(json.dumps({"error": str(exc), "execution_started": False}, ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = report()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Claudex Bridge: blocked (native integration has not passed)")
        for name, component in result["components"].items():
            print(f"  {name}: {component['version'] or 'not detected'}")
        for blocker in result["blockers"]:
            print(f"  [{blocker['code']}] {blocker['reason']}")
        print("No model calls, client changes, or task submissions were made.")
    return 0 if result["release_ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
