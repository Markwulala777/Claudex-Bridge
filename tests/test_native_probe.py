"""A diagnostic must not confuse artifacts, cached flags, or secrets with support."""
import gzip
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))

import bridge
import native_probe as probe


def write_asar(path, files):
    tree = {"files": {}}
    payload = bytearray()
    for name, content in files.items():
        raw = content.encode("utf-8")
        node = tree
        parts = name.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"offset": str(len(payload)), "size": len(raw)}
        payload.extend(raw)
    header = json.dumps(tree).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4I", 4, len(header) + 8, len(header) + 4, len(header)) + header + payload)


class NativeProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_asar_protocol_observation_and_mismatch(self):
        archive = self.root / "resources/app.asar"
        versions = dict(probe.IPC_VERSIONS)
        for expected in (True, False):
            with self.subTest(matches=expected):
                if not expected:
                    versions["thread-follower-interrupt-turn"] += 1
                write_asar(archive, {
                    "package.json": json.dumps({"version": "test.1"}),
                    ".vite/build/src-test.js": json.dumps(versions, separators=(",", ":")),
                })
                observed = probe.inspect_desktop(archive.parent, "codex")
                self.assertEqual(observed["version"], "test.1")
                self.assertEqual(observed["protocol_match"], expected)

    def test_truncated_archive_is_unknown_not_matched(self):
        path = self.root / "resources/app.asar"
        write_asar(path, {"package.json": '{"version":"1"}', ".vite/build/src-test.js": "x"})
        path.write_bytes(path.read_bytes()[:-10])
        result = probe.inspect_desktop(path.parent, "codex")
        self.assertTrue(result["installed"])
        self.assertIsNone(result["protocol_match"])
        self.assertIn("inspection_error", result)

    def test_extension_versions_use_numeric_order(self):
        for version in ("2.9.0", "2.10.0"):
            extension = self.root / ".vscode/extensions" / ("anthropic.claude-code-" + version)
            extension.mkdir(parents=True)
            (extension / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
        self.assertEqual(probe.inspect_extension(self.root, "anthropic.claude-code", "extension.js")["version"], "2.10.0")

    def write_cache(self, enabled):
        cache = self.root / "cache"
        data = {"features": {probe.TAIL_GATE: {"on": enabled, "secret": "private-account"}},
                "timestamp": 1_000_000, "organization": "private-organization"}
        cache.write_bytes(probe.FCACHE_MAGIC + gzip.compress(json.dumps(data).encode("utf-8")))
        return cache

    def test_gate_cache_exposes_only_selected_boolean_and_age(self):
        result = probe.cached_claude_tail_gate(self.write_cache(False), timestamp=1010)
        self.assertEqual(result, {"state": "off", "basis": "disk_cache_only", "age_seconds": 10})
        self.assertNotIn("private-", json.dumps(result))

    def test_gate_cache_does_not_coerce_strings_or_claim_live_state(self):
        result = probe.cached_claude_tail_gate(self.write_cache("false"), timestamp=1010)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["basis"], "disk_cache_only")

    def test_bad_cache_and_missing_cache_are_unknown(self):
        path = self.root / "cache"
        self.assertEqual(probe.cached_claude_tail_gate(path)["state"], "unknown")
        for raw in (b"garbage", probe.FCACHE_MAGIC + b"truncated gzip",
                    probe.FCACHE_MAGIC + bytes.fromhex("1f8b08000000000000ff") + b"\xff" * 20):
            path.write_bytes(raw)
            self.assertEqual(probe.cached_claude_tail_gate(path)["state"], "unknown")

    def test_provider_values_never_appear_in_report(self):
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://secret-provider.example/private-token",
            "ANTHROPIC_AUTH_TOKEN": "secret-credential",
        }}), encoding="utf-8")
        with patch.object(probe, "desktop_resources", return_value=None):
            result = probe.report(home=self.root, env={}, platform="linux")
        self.assertTrue(result["observations"]["claude_base_url_override_configured"])
        self.assertNotIn("secret-", json.dumps(result))
        self.assertNotIn("private-token", json.dumps(result))

    def test_detection_does_not_turn_into_verified_support_or_write_files(self):
        before = set(self.root.rglob("*"))
        with patch.object(probe, "desktop_resources", return_value=None), \
                patch.object(probe, "socket_exists", return_value=True), \
                patch.object(probe, "cached_claude_tail_gate", return_value={"state": "on", "basis": "disk_cache_only"}):
            result = probe.report(home=self.root, platform="linux", env={})
        self.assertFalse(result["release_ready"])
        self.assertGreater(len(result["blockers"]), 0)
        self.assertEqual(before, set(self.root.rglob("*")))

    def test_doctor_runs_without_linux_executor_on_other_platforms(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), patch.object(probe, "desktop_resources", return_value=None):
                result = probe.report(home=self.root, env={}, platform=platform)
                self.assertFalse(result["release_ready"])
                self.assertEqual(result["platform"], platform)

    def test_doctor_cli_reports_blocked_as_nonzero(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(bridge, "report", return_value={"release_ready": False, "blockers": ["test"]}), \
                contextlib.redirect_stdout(output):
            code = bridge.main(["doctor", "--json"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())["release_ready"])


if __name__ == "__main__":
    unittest.main()
