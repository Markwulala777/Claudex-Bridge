#!/usr/bin/env python3
"""Read one verified Claude transcript for the Desktop follower, without a model.

Only actual source messages are emitted. EOF, API-error text and process existence
are not execution-state observations. The native desktop's owner state therefore
remains unknown until a separate execution adapter can supply authoritative events.
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
    try:
        identity = ":".join(str(part) for part in follower.tail.identity)
        def emit(kind, messages):
            nonlocal sequence
            frame = json.dumps({
                "kind": kind, "session_id": binding.session_id,
                "source_identity": identity, "claude_code_version": binding.claude_code_version,
                "sequence": sequence, "messages": messages,
            }, ensure_ascii=False, separators=(",", ":"))
            if len(frame.encode("utf-8")) > MAX_FRAME_BYTES:
                raise ProtocolError("Desktop transcript snapshot/batch exceeds the supported 16 MiB frame limit")
            output.write(frame + "\n")
            output.flush()
            sequence += len(messages)
        emit("snapshot", follower.replay)
        follower.replay = []
        follower.initialized = True
        while True:
            messages = follower.poll()
            if messages:
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
