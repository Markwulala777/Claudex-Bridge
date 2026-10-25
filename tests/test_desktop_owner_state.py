import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"))
import claude_desktop_source as source


@unittest.skipUnless(sys.platform == "linux", "Native lifecycle reader uses Linux owner evidence")
class DesktopOwnerStateTests(unittest.TestCase):
    def fixture(self, root):
        session = "00000000-0000-4000-8000-000000000001"
        transcript = root / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "user", "sessionId": session, "version": "2.1.295",
            "uuid": "00000000-0000-4000-8000-000000000002", "parentUuid": None,
            "message": {"role": "user", "content": "Real fixture task"}}) + "\n")
        owner = root / "owner"
        owner.mkdir()
        binding = root / "binding.json"
        binding.write_text(json.dumps({"schema": 1, "session_id": session, "source": str(transcript),
            "source_format": "transcript", "claude_code_version": "2.1.295", "history": "replay",
            "owner_state_dir": str(owner)}))
        return session, binding, owner

    def test_real_lifecycle_state_changes_need_no_new_transcript_message(self):
        with tempfile.TemporaryDirectory() as directory:
            session, binding, owner = self.fixture(Path(directory))
            output = io.StringIO()
            with mock.patch("claude_owner.view_state", side_effect=[{"state": "running"}, {"state": "failed"}]) as observe:
                with mock.patch.object(source.time, "sleep", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        source.stream(str(binding), output)
            frames = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([frame["owner_state"] for frame in frames], ["running", "failed"])
            self.assertEqual(frames[1]["messages"], [])
            self.assertEqual(frames[1]["sequence"], 1)
            self.assertEqual(observe.call_args, mock.call(owner, session))

    def test_owner_identity_failure_never_emits_an_idle_snapshot(self):
        from claude_owner import OwnerError
        with tempfile.TemporaryDirectory() as directory:
            _, binding, _ = self.fixture(Path(directory))
            output = io.StringIO()
            with mock.patch("claude_owner.view_state", side_effect=OwnerError("identity changed")):
                with self.assertRaises(source.ProtocolError):
                    source.stream(str(binding), output)
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
