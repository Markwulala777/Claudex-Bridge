import argparse
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"))
import execution_adapters as adapters
import workflow_cli


@unittest.skipUnless(sys.platform == "linux", "Native adapters are Linux only")
class ClaudeNormalizationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("claude_owner.OwnerController")
        self.controller = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.controller.binding = {"session_id": "fixed-session"}
        self.adapter = adapters.ClaudeWorkflowOwner("fixed-session", "/unused/private")

    def test_exact_native_failure_stays_failed(self):
        evidence = {"status": "failed", "path": "/fixture/event", "sha256": "fixture"}
        self.controller.observe.return_value = {
            "session_id": "fixed-session", "request_id": "request", "turn_id": "real-turn",
            "status": "error", "completion": {"turn_id": "real-turn", "answer": "", "evidence": evidence}}
        result = self.adapter.observe("request", "real-turn")
        self.assertEqual(result["state"], "terminal")
        self.assertEqual(result["terminal_status"], "failed")
        self.assertIs(result["evidence"], evidence)

    def test_denied_without_turn_never_fabricates_completion(self):
        self.controller.observe.return_value = {
            "session_id": "fixed-session", "request_id": "request", "turn_id": None, "status": "denied"}
        result = self.adapter.observe("request")
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["execution_id"])
        self.assertNotIn("terminal_status", result)

    def test_wrong_observation_identity_is_rejected(self):
        self.controller.observe.return_value = {
            "session_id": "other", "request_id": "request", "turn_id": "real-turn", "status": "completed"}
        with self.assertRaises(adapters.AdapterError):
            self.adapter.observe("request")

    def test_missing_completion_evidence_is_not_success(self):
        self.controller.observe.return_value = {
            "session_id": "fixed-session", "request_id": "request", "turn_id": "real-turn",
            "completion": {"turn_id": "real-turn", "answer": "looks done"}}
        with self.assertRaisesRegex(adapters.AdapterError, "durable evidence"):
            self.adapter.observe("request")

    def test_transport_cannot_switch_executor_or_add_options(self):
        task = {"executor": {"product": "claude", "session_id": "fixed-session"}}
        for transport in ({"kind": "codex-desktop", "rollout_path": "/fixture"},
                          {"kind": "claude-native", "owner_state_dir": "/fixture", "auto_login": True}):
            with self.assertRaises(adapters.AdapterError):
                adapters.factory(task, transport, "/unused/attempt")

    def test_native_factories_require_and_forward_the_fixed_project(self):
        with tempfile.TemporaryDirectory() as project:
            task = {"project": project, "executor": {"product": "claude", "session_id": "fixed-session"}}
            adapters.factory(task, {"kind": "claude-native", "owner_state_dir": "/fixture"}, "/unused/attempt")
            import claude_owner
            claude_owner.OwnerController.assert_called_with("/fixture", expected_project=project)
            task["executor"]["product"] = "codex"
            with mock.patch("codex_owner.CodexOwner") as codex:
                adapters.factory(task, {"kind": "codex-desktop", "rollout_path": "/fixture"}, "/unused/attempt")
                codex.assert_called_with("fixed-session", "/fixture", "/unused/attempt", expected_project=project)
            del task["project"]
            with self.assertRaisesRegex(adapters.AdapterError, "task project"):
                adapters.factory(task, {"kind": "codex-desktop", "rollout_path": "/fixture"}, "/unused/attempt")


class CLIGateTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Runtime gate is Linux specific")
    def test_dispatch_stops_before_state_or_model_when_acceptance_is_missing(self):
        args = argparse.Namespace(command="task-dispatch")
        with mock.patch("native_probe.report", return_value={
                "runtime_ready": False, "runtime_blockers": [{"code": "native_acceptance"}]}):
            result = workflow_cli.run(args)
        self.assertFalse(result["execution_started"])
        self.assertEqual(result["status"], "blocked")

    def test_unimplemented_platform_does_not_import_native_execution(self):
        with mock.patch.object(workflow_cli.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "only for Linux"):
                workflow_cli.run(argparse.Namespace(command="task-dispatch"))


if __name__ == "__main__":
    unittest.main()
