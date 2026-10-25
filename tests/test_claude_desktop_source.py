"""Exercise the real read-only source subprocess with temporary transcripts.

No model, GUI, IPC, login, or user-owned conversation is opened by these tests.
"""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


SCRIPT = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts/claude_desktop_source.py"
SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
EOF = object()


def transcript(kind="user", text="fixture prompt", parent=None, **extra):
    row = {
        "type": kind, "sessionId": SESSION, "version": "2.1.295",
        "uuid": str(uuid.uuid4()), "parentUuid": parent, "isSidechain": False,
        "timestamp": "2026-10-25T00:00:00Z",
        "message": {"role": kind, "content": text},
    }
    if kind == "assistant":
        row["message"].update({"id": "msg_fixture", "type": "message", "model": "fixture-model"})
    row.update(extra)
    return row


def wire(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class DesktopSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "owner.jsonl"
        self.source.touch()
        self.binding = self.root / "binding.json"

    def append(self, *rows):
        with self.source.open("ab") as handle:
            for row in rows:
                handle.write(wire(row))

    def start(self, max_frame=None, **changes):
        binding = {
            "schema": 1, "session_id": SESSION, "source": str(self.source),
            "source_format": "transcript", "history": "replay",
            "claude_code_version": "2.1.295",
        }
        binding.update(changes)
        self.binding.write_text(json.dumps(binding), encoding="utf-8")
        command = [sys.executable, str(SCRIPT), "--binding", str(self.binding)]
        if max_frame is not None:
            # Only the byte limit is reduced, inside a real isolated subprocess.
            # Exercise the actual framing path without a 16 MiB fixture.
            bootstrap = """
import importlib.util, pathlib, sys
sys.dont_write_bytecode = True
script, binding, limit = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).parent))
spec = importlib.util.spec_from_file_location('desktop_source_fixture', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.MAX_FRAME_BYTES = int(limit)
sys.exit(module.main(['--binding', binding]))
"""
            command = [sys.executable, "-c", bootstrap, str(SCRIPT), str(self.binding), str(max_frame)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
        process.source_pending = b""
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for pipe in (process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

    def frame(self, process, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            if b"\n" in process.source_pending:
                line, process.source_pending = process.source_pending.split(b"\n", 1)
                return json.loads(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                return None
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                self.assertFalse(process.source_pending, "Source exited with a partial JSON frame")
                return EOF
            process.source_pending += data

    def assert_waiting(self, process):
        self.assertIsNone(self.frame(process, timeout=0.35), "EOF produced an unsolicited frame or exit")
        self.assertIsNone(process.poll(), "Source must wait for further owner messages")

    def assert_failure(self, process):
        frame = self.frame(process)
        self.assertIsInstance(frame, dict)
        self.assertEqual(frame["kind"], "source_error")
        self.assertTrue(frame["error"])
        self.assertEqual(process.wait(timeout=3), 2)
        self.assertIs(self.frame(process), EOF)
        self.assertNotIn(b"Traceback", process.stderr.read())
        return frame

    def test_snapshot_and_append_have_one_stable_source_and_contiguous_sequence(self):
        first = transcript()
        self.append(first)
        process = self.start()
        initial = self.frame(process)
        self.assertEqual(initial["kind"], "snapshot")
        self.assertEqual(initial["sequence"], 0)
        self.assertEqual([row["uuid"] for row in initial["messages"]], [first["uuid"]])
        info = self.source.stat()
        self.assertEqual(initial["source_identity"], "{}:{}".format(info.st_dev, info.st_ino))
        self.assertEqual(initial["session_id"], SESSION)
        self.assertEqual(initial["claude_code_version"], "2.1.295")
        self.assertNotIn("owner_state", initial)
        second = transcript("assistant", "fixture delivery", parent=first["uuid"])
        third = transcript("user", "fixture feedback", parent=second["uuid"])
        self.append(second, third)
        appended = self.frame(process)
        self.assertEqual(appended["kind"], "append")
        self.assertEqual(appended["sequence"], 1)
        self.assertEqual(appended["source_identity"], initial["source_identity"])
        self.assertEqual([row["uuid"] for row in appended["messages"]], [second["uuid"], third["uuid"]])
        self.assertTrue(all(row["session_id"] == SESSION for row in appended["messages"]))
        self.append(third)  # An identical recorded row is not delivered twice.
        self.assert_waiting(process)

    def test_partial_utf8_record_is_not_emitted_until_its_newline(self):
        first = transcript()
        self.append(first)
        process = self.start()
        self.assertEqual(self.frame(process)["kind"], "snapshot")
        second = transcript("assistant", "中文 fixture", parent=first["uuid"])
        data = wire(second)
        split = data.index("中".encode("utf-8")) + 1
        with self.source.open("ab") as handle:
            handle.write(data[:split])
        self.assert_waiting(process)
        with self.source.open("ab") as handle:
            handle.write(data[split:])
        frame = self.frame(process)
        self.assertEqual(frame["sequence"], 1)
        self.assertEqual(frame["messages"][0]["message"]["content"], "中文 fixture")

    def test_replaced_source_fails_instead_of_attaching_to_the_new_file(self):
        self.append(transcript())
        process = self.start()
        self.assertEqual(self.frame(process)["kind"], "snapshot")
        replacement = self.root / "replacement.jsonl"
        replacement.write_bytes(wire(transcript(text="must not be delivered")))
        os.replace(replacement, self.source)
        error = self.assert_failure(process)
        self.assertIn("replaced", error["error"])

    def test_truncated_source_fails_without_a_completion_event(self):
        self.append(transcript())
        process = self.start()
        self.assertEqual(self.frame(process)["kind"], "snapshot")
        self.source.write_bytes(b"")
        error = self.assert_failure(process)
        self.assertIn("truncated", error["error"])

    def test_session_and_producer_changes_are_rejected_after_snapshot(self):
        for changed in ({"sessionId": OTHER}, {"version": "2.1.308"}):
            with self.subTest(changed=changed):
                self.source.write_bytes(b"")
                first = transcript()
                self.append(first)
                process = self.start()
                self.assertEqual(self.frame(process)["kind"], "snapshot")
                self.append(transcript("assistant", parent=first["uuid"], **changed))
                self.assert_failure(process)

    def test_unproven_source_or_binding_never_emits_a_snapshot(self):
        cases = [([], {}), ([transcript(sessionId=OTHER)], {}),
                 ([transcript(version="2.1.308")], {}),
                 ([transcript()], {"history": "native", "start_offset": 0}),
                 ([transcript()], {"claude_code_version": "2.1.308"})]
        for rows, changes in cases:
            with self.subTest(changes=changes, row_count=len(rows)):
                self.source.write_bytes(b"")
                self.append(*rows)
                self.assert_failure(self.start(**changes))

    def test_error_text_and_eof_are_not_converted_into_result_or_state(self):
        first = transcript()
        failure = transcript("assistant", "API Error: 403 quota exhausted", parent=first["uuid"])
        self.append(first, failure)
        original = self.source.read_bytes()
        process = self.start()
        snapshot = self.frame(process)
        self.assertEqual(snapshot["messages"][1]["message"]["content"], failure["message"]["content"])
        self.assertEqual([row["type"] for row in snapshot["messages"]], ["user", "assistant"])
        self.assertNotIn("owner_state", snapshot)
        self.assert_waiting(process)
        self.assertEqual(self.source.read_bytes(), original)

    def test_termination_reaps_the_reader_and_leaves_the_source_untouched(self):
        self.append(transcript())
        original = self.source.read_bytes()
        process = self.start()
        self.assertEqual(self.frame(process)["kind"], "snapshot")
        self.stop(process)
        self.assertIsNotNone(process.returncode)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"owner.jsonl", "binding.json"})

    def test_closed_stdout_exits_without_recursive_error_writes(self):
        for invalid_source in (False, True):
            with self.subTest(invalid_source=invalid_source):
                self.source.write_bytes(b"")
                first = transcript()
                self.append(first)
                process = self.start()
                self.assertEqual(self.frame(process)["kind"], "snapshot")
                process.stdout.close()
                extra = {"version": "2.1.308"} if invalid_source else {}
                self.append(transcript("assistant", parent=first["uuid"], **extra))
                self.assertNotEqual(process.wait(timeout=3), 0)
                stderr = process.stderr.read()
                self.assertNotIn(b"Traceback", stderr)
                self.assertNotIn(b"Exception ignored", stderr)

    def test_frame_limit_is_checked_before_snapshot_or_append_is_written(self):
        for initial_large in (True, False):
            with self.subTest(initial_large=initial_large):
                self.source.write_bytes(b"")
                first = transcript(text="x" * 2000 if initial_large else "small fixture")
                self.append(first)
                process = self.start(max_frame=1024)
                if not initial_large:
                    self.assertEqual(self.frame(process)["kind"], "snapshot")
                    self.append(transcript("assistant", "x" * 2000, parent=first["uuid"]))
                error = self.assert_failure(process)
                self.assertLessEqual(len(wire(error)), 1024)
                self.assertTrue("limit" in error["error"].lower() or "large" in error["error"].lower())


if __name__ == "__main__":
    unittest.main()
