"""A diagnostic must not confuse artifacts, cached flags, or secrets with support."""
import gzip
import contextlib
import copy
import hashlib
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

    @contextlib.contextmanager
    def installed_runtime(self, implemented=False):
        """Installation and future capability doubles are not live acceptance."""
        def desktop(_resources, product):
            return {"installed": True, "version": product + "-desktop-test.1", "protocol_match": True}

        def extension(_home, prefix, _code_file):
            return {"installed": True, "version": prefix + "-extension-test.1", "protocol_match": True}

        capabilities = {name: implemented for name in probe.IMPLEMENTATION_CAPABILITIES}
        with patch.object(probe, "desktop_resources", return_value=None), \
                patch.object(probe, "inspect_desktop", side_effect=desktop), \
                patch.object(probe, "inspect_extension", side_effect=extension), \
                patch.object(probe, "host_fingerprint", return_value="1" * 64), \
                patch.object(probe, "implementation_fingerprint", return_value="2" * 64), \
                patch.object(probe, "IMPLEMENTATION_CAPABILITIES", capabilities):
            yield

    def write_receipt(self, binding):
        path = probe.acceptance_path(self.root, {})
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"schema_version": 1, "kind": "native_workflow_acceptance", "binding": binding,
                   "run_id": "00000000-0000-4000-8000-000000000001", "verified_at": "2000-01-01T00:00:00Z",
                   "directions": {}}
        for direction in probe.ACCEPTANCE_DIRECTIONS:
            scenarios = {}
            for scenario in probe.ACCEPTANCE_SCENARIOS:
                artifact = direction + "-" + scenario + ".json"
                raw = json.dumps({"source": "explicit offline test fixture", "direction": direction,
                                  "scenario": scenario}).encode()
                (path.parent / artifact).write_bytes(raw)
                (path.parent / artifact).chmod(0o600)
                scenarios[scenario] = {"result": "passed", "artifact": artifact,
                                       "sha256": hashlib.sha256(raw).hexdigest()}
            receipt["directions"][direction] = {
                "sessions": {"claude": "00000000-0000-4000-8000-000000000002",
                             "codex": "00000000-0000-4000-8000-000000000003"},
                "scenarios": scenarios}
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
        return path, receipt

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
        self.assertFalse(result["runtime_ready"])
        self.assertGreater(len(result["blockers"]), 0)
        self.assertEqual(before, set(self.root.rglob("*")))

    def test_doctor_runs_without_linux_executor_on_other_platforms(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform), patch.object(probe, "desktop_resources", return_value=None):
                result = probe.report(home=self.root, env={}, platform=platform)
                self.assertFalse(result["release_ready"])
                self.assertFalse(result["runtime_ready"])
                self.assertEqual(result["platform"], platform)

    def test_complete_receipt_cannot_create_missing_implementation(self):
        with self.installed_runtime():
            binding = probe.report(home=self.root, platform="linux", env={})["local_acceptance"]["binding"]
            path, receipt = self.write_receipt(binding)
            receipt["implementation_capabilities"] = {name: True for name in probe.IMPLEMENTATION_CAPABILITIES}
            path.write_text(json.dumps(receipt))
            result = probe.report(home=self.root, platform="linux", env={})
        self.assertTrue(result["local_acceptance"]["verified"])
        self.assertFalse(result["runtime_ready"])
        self.assertFalse(result["release_ready"])
        codes = {item["code"] for item in result["runtime_blockers"]}
        self.assertTrue(set(probe.IMPLEMENTATION_CAPABILITIES).issubset(codes))
        self.assertFalse(any(result["implementation_capabilities"].values()))

    def test_installed_future_implementation_still_needs_full_local_acceptance(self):
        with self.installed_runtime(implemented=True):
            result = probe.report(home=self.root, platform="linux", env={})
        self.assertFalse(result["runtime_ready"])
        self.assertEqual([item["code"] for item in result["runtime_blockers"]], ["local_acceptance"])
        self.assertEqual(result["local_acceptance"]["state"], "missing")

    def test_verified_linux_can_be_development_ready_without_cross_platform_release(self):
        with self.installed_runtime(implemented=True):
            binding = probe.report(home=self.root, platform="linux", env={})["local_acceptance"]["binding"]
            self.write_receipt(binding)
            before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
            result = probe.report(home=self.root, platform="linux", env={})
        self.assertTrue(result["runtime_ready"])
        self.assertFalse(result["release_ready"])
        self.assertEqual(result["status"], "development_ready")
        self.assertEqual(result["runtime_blockers"], [])
        self.assertEqual(result["platform_blockers"][0]["platforms"], ["win32", "darwin"])
        self.assertEqual(result["blockers"], result["runtime_blockers"] + result["platform_blockers"])
        self.assertEqual(result["local_acceptance"]["checked_scenarios"],
                         len(probe.ACCEPTANCE_DIRECTIONS) * len(probe.ACCEPTANCE_SCENARIOS))
        self.assertFalse(result["development_evidence"]["full_bridge_verified"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_single_true_stale_binding_and_partial_scenarios_do_not_enable_runtime(self):
        with self.installed_runtime(implemented=True):
            binding = probe.report(home=self.root, platform="linux", env={})["local_acceptance"]["binding"]
            path, original = self.write_receipt(binding)
            variants = [{"runtime_ready": True, "release_ready": True, "passed": True}]
            for key in ("host_fingerprint", "implementation_fingerprint", "client_versions", "platform"):
                candidate = copy.deepcopy(original)
                candidate["binding"][key] = "stale"
                variants.append(candidate)
            candidate = copy.deepcopy(original)
            del candidate["directions"]["codex_to_claude"]
            variants.append(candidate)
            candidate = copy.deepcopy(original)
            candidate["directions"]["claude_to_codex"]["scenarios"][probe.ACCEPTANCE_SCENARIOS[0]]["result"] = "failed"
            variants.append(candidate)
            candidate = copy.deepcopy(original)
            candidate["verified_at"] = "2999-01-01T00:00:00Z"
            variants.append(candidate)
            for index, candidate in enumerate(variants):
                with self.subTest(variant=index):
                    path.write_text(json.dumps(candidate))
                    result = probe.report(home=self.root, platform="linux", env={})
                    self.assertFalse(result["runtime_ready"])
                    self.assertFalse(result["local_acceptance"]["verified"])

    def test_changed_or_escaping_evidence_is_rejected_without_echoing_private_content(self):
        with self.installed_runtime(implemented=True):
            binding = probe.report(home=self.root, platform="linux", env={})["local_acceptance"]["binding"]
            path, original = self.write_receipt(binding)
            scenario = probe.ACCEPTANCE_SCENARIOS[0]
            entry = original["directions"]["claude_to_codex"]["scenarios"][scenario]
            artifact = path.parent / entry["artifact"]
            previous = artifact.read_bytes()
            artifact.write_text("private-token-changed-content")
            result = probe.report(home=self.root, platform="linux", env={})
            self.assertFalse(result["runtime_ready"])
            self.assertNotIn("private-token", json.dumps(result))
            artifact.write_bytes(previous)
            entry["artifact"] = "../private-token-outside.json"
            path.write_text(json.dumps(original))
            result = probe.report(home=self.root, platform="linux", env={})
            self.assertFalse(result["runtime_ready"])
            self.assertNotIn("private-token", json.dumps(result))

    def test_acceptance_path_retains_old_state_root_and_creates_nothing(self):
        root = self.root / "existing-worker-state"
        path = probe.acceptance_path(self.root, {"CODEX_WORKER_STATE_DIR": str(root)})
        self.assertEqual(path, root / "native-acceptance.json")
        self.assertFalse(root.exists())
        override = self.root / "explicit.json"
        self.assertEqual(probe.acceptance_path(self.root, {"CLAUDEX_ACCEPTANCE_EVIDENCE": str(override)}), override)

    def test_machine_binding_changes_for_home_or_codex_state_without_exposing_identifiers(self):
        with patch.object(Path, "read_text", return_value="a" * 32), patch.object(probe.os, "getuid", return_value=1000):
            first = probe.host_fingerprint(self.root, "linux", self.root / ".codex")
            second = probe.host_fingerprint(self.root / "another-home", "linux", self.root / ".codex")
            third = probe.host_fingerprint(self.root, "linux", self.root / "another-codex")
        self.assertEqual(len(first), 64)
        self.assertEqual(len({first, second, third}), 3)
        self.assertIsNone(probe.host_fingerprint(self.root, "darwin", self.root / ".codex"))

    def test_implementation_fingerprint_changes_after_adapter_or_skill_edits(self):
        scripts = self.root / "skill/scripts"
        scripts.mkdir(parents=True)
        module = scripts / "native_probe.py"
        module.write_text("fixture adapter v1")
        skill = scripts.parent / "SKILL.md"
        skill.write_text("fixture policy v1")
        with patch.object(probe, "__file__", str(module)):
            first = probe.implementation_fingerprint()
            module.write_text("fixture adapter v2")
            second = probe.implementation_fingerprint()
            skill.write_text("fixture policy v2")
            third = probe.implementation_fingerprint()
            (scripts / "loader.js").write_text("fixture loader v1")
            fourth = probe.implementation_fingerprint()
            (scripts / "protocol.json").write_text('{"version":1}')
            fifth = probe.implementation_fingerprint()
            references = scripts.parent / "references"
            references.mkdir()
            (references / "workflow.md").write_text("review instructions")
            sixth = probe.implementation_fingerprint()
            module.chmod(0o755)
            seventh = probe.implementation_fingerprint()
            (scripts.parent / ".claudex-install.json").write_text('{"schema":1}')
            self.assertEqual(probe.implementation_fingerprint(), seventh)
        self.assertEqual(len({first, second, third, fourth, fifth, sixth, seventh}), 7)

    def test_doctor_cli_reports_blocked_as_nonzero(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch.object(bridge, "report", return_value={"runtime_ready": False, "release_ready": False, "blockers": ["test"]}), \
                contextlib.redirect_stdout(output):
            code = bridge.main(["doctor", "--json"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())["release_ready"])
        with patch.object(bridge, "report", return_value={"runtime_ready": True, "release_ready": False}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(bridge.main(["doctor", "--json"]), 0)
            self.assertEqual(bridge.main(["doctor", "--json", "--require-release"]), 2)


if __name__ == "__main__":
    unittest.main()
