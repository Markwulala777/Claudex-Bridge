#!/usr/bin/env python3
"""Claudex Bridge development CLI. No live bridge is dispatched by doctor."""
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
    args = parser.parse_args(argv)
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
