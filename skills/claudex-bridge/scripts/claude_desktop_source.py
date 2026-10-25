#!/usr/bin/env python3
"""Read one verified Claude transcript for the Desktop follower, without a model.

Only actual source messages are emitted. An optional exact-session native hook
binding supplies owner state; EOF and API-error text never establish completion.
"""
import argparse
import json
import os
import sys
import time

sys.dont_write_bytecode = True
from claude_vscode_proxy import Binding, Follower, ProtocolError
MAX_FRAME_BYTES = 16 * 1024 * 1024


def stream(binding_path, output=sys.stdout):
    binding = Binding.load(binding_path, check_ready_file=False)
    if binding.history != "replay" or binding.source_format != "transcript":
        raise ProtocolError("Desktop source requires one transcript with replay history")
    follower = Follower(binding)
    sequence = 0
    owner_state = None
    try:
        def observed_state():
            if binding.owner_state_dir is None:
                return None
            from claude_owner import OwnerError, view_state
            try:
                return view_state(binding.owner_state_dir, binding.session_id)["state"]
            except (OwnerError, OSError, ValueError) as exc:
                raise ProtocolError("Bound owner lifecycle is unavailable: " + str(exc)) from None
        identity = ":".join(str(part) for part in follower.tail.identity)
        def emit(kind, messages):
            nonlocal sequence
            frame = json.dumps({
                "kind": kind, "session_id": binding.session_id,
                "source_identity": identity, "claude_code_version": binding.claude_code_version,
                "sequence": sequence, "messages": messages,
                **({"owner_state": owner_state} if owner_state is not None else {}),
            }, ensure_ascii=False, separators=(",", ":"))
            if len(frame.encode("utf-8")) > MAX_FRAME_BYTES:
                raise ProtocolError("Desktop transcript snapshot/batch exceeds the supported 16 MiB frame limit")
            output.write(frame + "\n")
            output.flush()
            sequence += len(messages)
        owner_state = observed_state()
        emit("snapshot", follower.replay)
        follower.replay = []
        follower.initialized = True
        while True:
            messages = follower.poll()
            current_state = observed_state()
            changed = current_state != owner_state
            owner_state = current_state
            if messages or changed:
                emit("append", messages)
            time.sleep(0.1)
    finally:
        follower.close()


def close_broken_stdout():
    # Suppress Python's final flush as well as the immediate failed write.
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(fd, sys.stdout.fileno())
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    args = parser.parse_args(argv)
    try:
        stream(args.binding)
        return 0
    except BrokenPipeError:
        # Avoid a second write/traceback and Python's exit-time stdout flush.
        close_broken_stdout()
        return 130
    except (ProtocolError, OSError) as exc:
        try:
            print(json.dumps({"kind": "source_error", "error": str(exc)}), flush=True)
        except BrokenPipeError:
            close_broken_stdout()
            return 130
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
