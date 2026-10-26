"""Read-only native client observations. Detection is never live validation."""
import gzip
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time
import uuid
import zlib


IPC_VERSIONS = {
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-load-complete-history": 1,
    "thread-follower-interrupt-turn": 4,
    "thread-stream-state-changed": 11,
    "thread-stream-following-changed": 1,
    "thread-stream-following-status-requested": 1,
}
FCACHE_MAGIC = bytes.fromhex("434c4602009ab7e2")
TAIL_GATE = "1985233154"
MAX_READ = 64 * 1024 * 1024
MAX_RECEIPT = 1024 * 1024
MAX_EVIDENCE = 8 * 1024 * 1024

# These describe implemented, reviewed capabilities, not user configuration.
# A local acceptance receipt cannot enable missing implementation. Change a gate
# only with its implementation and acceptance; the code fingerprint then changes.
IMPLEMENTATION_CAPABILITIES = {
    "claude_native_follow": True,
    "native_workflow_acceptance": True,
    "bidirectional_acceptance": True,
}
ACCEPTANCE_DIRECTIONS = ("claude_to_codex", "codex_to_claude")
ACCEPTANCE_SCENARIOS = (
    "single_executor_and_native_views", "same_session_revision",
    "initiator_review_and_report", "feedback_preserves_context",
    "recovery_failure_and_stop_safety", "roles_subagents_and_existing_changes",
    "configuration_preserved",
)
NATIVE_COMPONENTS = ("codex_desktop", "claude_desktop", "codex_vscode", "claude_vscode")


class Asar:
    def __init__(self, path):
        self.path = Path(path)
        with self.path.open("rb") as stream:
            raw = stream.read(16)
            if len(raw) != 16:
                raise ValueError("Incomplete ASAR header")
            header = struct.unpack("<4I", raw)
            if not 0 < header[3] <= MAX_READ or header[1] < header[3] + 8:
                raise ValueError("Unsupported ASAR header")
            self.tree = json.loads(stream.read(header[3]))
            if not isinstance(self.tree, dict) or not isinstance(self.tree.get("files"), dict):
                raise ValueError("Unsupported ASAR directory")
            self.base = 8 + header[1]

    def node(self, name):
        node = self.tree
        for part in name.split("/"):
            node = node["files"][part]
        return node

    def names(self, directory):
        return tuple(self.node(directory)["files"])

    def read(self, name):
        node = self.node(name)
        size = node["size"]
        if not isinstance(size, int) or not 0 <= size <= MAX_READ:
            raise ValueError("Unsupported ASAR entry size")
        if node.get("unpacked"):
            path = Path(str(self.path) + ".unpacked") / name
            with path.open("rb") as stream:
                raw = stream.read(size)
        else:
            offset = int(node["offset"])
            if offset < 0:
                raise ValueError("Invalid ASAR entry offset")
            with self.path.open("rb") as stream:
                stream.seek(self.base + offset)
                raw = stream.read(size)
        if len(raw) != size:
            raise ValueError("Incomplete ASAR entry")
        return raw.decode("utf-8")


def versions_in(source):
    return {key: int(value) for key, value in
            re.findall(r'"(thread-[^"]+)":(\d+)', source)}


def ipc_matches(versions):
    return all(versions.get(key) == version for key, version in IPC_VERSIONS.items())


def desktop_resources(product, home, platform, env):
    override = env.get("CLAUDEX_" + product.upper() + "_DESKTOP_RESOURCES")
    if product == "codex":
        override = override or env.get("CODEX_WORKER_DESKTOP_RESOURCES")
    if override:
        return Path(override).expanduser()
    if platform == "linux":
        choices = ([Path("/opt/codex-desktop/resources")] if product == "codex" else
                   [Path("/usr/lib/claude-desktop/resources"), Path("/opt/Claude/resources")])
    elif platform == "darwin":
        app = "Codex.app" if product == "codex" else "Claude.app"
        choices = [home / "Applications" / app / "Contents/Resources",
                   Path("/Applications") / app / "Contents/Resources"]
    else:
        # Windows Store and per-user app layouts vary. Use the explicit resource
        # override when auto-discovery has no observed installation to inspect.
        return None
    return next((path for path in choices if (path / "app.asar").is_file()), None)


def inspect_desktop(resources, product):
    result = {"installed": False, "version": None, "protocol_match": None}
    if resources is None:
        return result
    archive = Path(resources) / "app.asar"
    result["installed"] = archive.is_file()
    if not result["installed"]:
        return result
    try:
        asar = Asar(archive)
        package = json.loads(asar.read("package.json"))
        if not isinstance(package, dict):
            raise ValueError("Unsupported desktop package metadata")
        result["version"] = package.get("version")
        if product == "codex":
            for name in asar.names(".vite/build"):
                if name.startswith("src-") and name.endswith(".js"):
                    versions = versions_in(asar.read(".vite/build/" + name))
                    if "thread-stream-state-changed" in versions:
                        result["protocol_match"] = ipc_matches(versions)
                        break
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["inspection_error"] = "Unsupported or unreadable installed archive"
    return result


def inspect_extension(home, prefix, code_file):
    candidates = []
    for folder in (home / ".vscode/extensions", home / ".vscode-insiders/extensions"):
        if not folder.is_dir():
            continue
        for path in folder.glob(prefix + "-*"):
            try:
                package = json.loads((path / "package.json").read_text(encoding="utf-8"))
                version = package["version"]
                order = tuple(int(piece) for piece in re.findall(r"\d+", version))
                candidates.append((order, version, path))
            except (OSError, ValueError, KeyError, TypeError):
                continue
    result = {"installed": bool(candidates), "version": None, "protocol_match": None}
    if candidates:
        _, result["version"], path = max(candidates, key=lambda item: item[0])
        if prefix == "openai.chatgpt":
            try:
                source = (path / code_file).read_text(encoding="utf-8")
                result["protocol_match"] = ipc_matches(versions_in(source))
            except (OSError, ValueError):
                result["inspection_error"] = "Unreadable installed extension"
    return result


def cached_claude_tail_gate(path, timestamp=None):
    result = {"state": "unknown", "basis": "disk_cache_only", "age_seconds": None}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_READ + 1)
        if len(raw) > MAX_READ or not raw.startswith(FCACHE_MAGIC):
            return result
        with gzip.GzipFile(fileobj=io.BytesIO(raw[8:])) as stream:
            decoded = stream.read(MAX_READ + 1)
        if len(decoded) > MAX_READ:
            return result
        data = json.loads(decoded)
        gate = data.get("features", {}).get(TAIL_GATE, {})
        enabled = gate.get("on")
        if type(enabled) is bool:
            result["state"] = "on" if enabled else "off"
        cached_at = data.get("timestamp")
        if type(cached_at) in (int, float):
            current = time.time() if timestamp is None else timestamp
            result["age_seconds"] = round(current - cached_at / 1000)
    except (OSError, EOFError, ValueError, TypeError, AttributeError, OverflowError, zlib.error):
        pass
    return result


def base_url_override(home, env):
    """Return a boolean only. Do not retain or print endpoint/credential values."""
    if env.get("ANTHROPIC_BASE_URL"):
        return True
    settings = home / ".claude/settings.json"
    if not settings.exists():
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        return bool(data.get("env", {}).get("ANTHROPIC_BASE_URL"))
    except (OSError, ValueError, AttributeError):
        return None


def socket_exists(path):
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def host_fingerprint(home, platform, codex_home):
    """Bind local validation without exposing machine IDs or user paths."""
    if platform != "linux":
        return None
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", machine_id):
            return None
        identity = [platform, machine_id.lower(), os.getuid(), str(home.resolve()), str(codex_home.resolve())]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    except (OSError, ValueError, AttributeError):
        return None


def implementation_fingerprint():
    """Bind all executable code and packaged Skill instructions, not its location."""
    directory = Path(__file__).resolve().parent.parent
    paths = sorted(path for path in directory.rglob("*")
                   if path.suffix in (".py", ".js", ".json", ".md", ".yaml", ".yml")
                   and path.name != ".claudex-install.json"
                   and "__pycache__" not in path.parts and path.is_file())
    digest = hashlib.sha256()
    try:
        for path in paths:
            raw = path.read_bytes()
            if len(raw) > MAX_READ:
                return None
            if path.is_symlink():
                return None
            digest.update(str(path.relative_to(directory)).encode() + b"\0")
            digest.update(bytes([path.stat().st_mode & 0o111]))
            digest.update(hashlib.sha256(raw).digest())
        return digest.hexdigest() if paths else None
    except (OSError, ValueError):
        return None


def acceptance_path(home, env):
    """Read beside the retained worker state; never create/migrate its directory."""
    if env.get("CLAUDEX_ACCEPTANCE_EVIDENCE"):
        return Path(env["CLAUDEX_ACCEPTANCE_EVIDENCE"]).expanduser()
    base = Path(env.get("XDG_STATE_HOME", str(home / ".local/state")))
    root = Path(env.get("CODEX_WORKER_STATE_DIR", str(base / "claude-codex-worker"))).expanduser()
    return root / "native-acceptance.json"


def read_evidence_file(path, limit):
    """Bound reads and reject symlinks/FIFOs without printing file contents."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("Evidence must be a bounded regular file")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("Evidence must be owned by this user and not writable by others")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(str(path), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("Evidence changed while opening")
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError("Evidence is empty or exceeds its size limit")
    return raw


def inspect_acceptance(path, binding, timestamp=None):
    """Validate a local acceptance receipt, not a `passed=true` feature flag.

    Schema 1: kind="native_workflow_acceptance", binding equal to the reported
    installation binding, canonical run_id, verified_at with a timezone, and
    directions[direction]={sessions:{claude:UUID,codex:UUID}, scenarios:{name:
    {result:"passed",artifact:"relative/file",sha256:"64 lowercase hex"}}}.
    Every required scenario in both directions needs a matching local artifact.
    This validates provenance/integrity; it does not rerun or semantically judge
    the recorded acceptance. Receipt creation is deliberately outside doctor.
    """
    result = {"verified": False, "state": "missing", "checked_scenarios": 0,
              "reason": "No matching local full-workflow acceptance receipt is available."}
    if not path.exists() and not path.is_symlink():
        return result
    result["state"] = "invalid"
    try:
        data = json.loads(read_evidence_file(path, MAX_RECEIPT))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported acceptance receipt schema")
        if data.get("kind") != "native_workflow_acceptance" or data.get("binding") != binding:
            raise ValueError("Acceptance receipt does not match this host, implementation or client versions")
        if (not binding.get("host_fingerprint") or not binding.get("implementation_fingerprint") or
                any(not version for version in binding["client_versions"].values())):
            raise ValueError("The current installation cannot be bound to acceptance evidence")
        for value in [data.get("run_id")]:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError("Acceptance run identity is invalid")
        verified_at = datetime.datetime.fromisoformat(data["verified_at"].replace("Z", "+00:00"))
        current = time.time() if timestamp is None else timestamp
        if verified_at.tzinfo is None or verified_at.timestamp() > current + 300:
            raise ValueError("Acceptance timestamp is missing a timezone or is in the future")
        root = path.parent.resolve()
        checked = {}
        for direction in ACCEPTANCE_DIRECTIONS:
            record = data["directions"][direction]
            for agent in ("claude", "codex"):
                value = record["sessions"][agent]
                if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                    raise ValueError("Acceptance session identity is invalid")
            for scenario in ACCEPTANCE_SCENARIOS:
                entry = record["scenarios"][scenario]
                if not isinstance(entry, dict) or entry.get("result") != "passed":
                    raise ValueError("Every required scenario must have recorded acceptance")
                name, expected = entry.get("artifact"), entry.get("sha256")
                if (not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts or
                        not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
                    raise ValueError("Acceptance artifact reference is invalid")
                artifact = root / name
                try:
                    artifact.resolve().relative_to(root)
                except ValueError:
                    raise ValueError("Acceptance artifact escapes its evidence directory") from None
                if name not in checked:
                    checked[name] = hashlib.sha256(read_evidence_file(artifact, MAX_EVIDENCE)).hexdigest()
                if checked[name] != expected:
                    raise ValueError("Acceptance artifact digest does not match")
                result["checked_scenarios"] += 1
    except (OSError, KeyError, TypeError, AttributeError, OverflowError, ValueError):
        # Neither file contents nor exception messages (which may embed paths or
        # untrusted receipt values) belong in a diagnostic report.
        result["reason"] = "Local acceptance evidence is malformed, incomplete, stale, unsafe, or has changed."
        return result
    result.update(verified=True, state="verified",
                  reason="Both directions have locally bound scenario evidence with matching digests.")
    return result


def report(home=None, platform=None, env=None):
    home = Path.home() if home is None else Path(home)
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    components = {
        product + "_desktop": inspect_desktop(
            desktop_resources(product, home, platform, env), product)
        for product in ("codex", "claude")
    }
    components["codex_vscode"] = inspect_extension(home, "openai.chatgpt", "out/extension.js")
    components["claude_vscode"] = inspect_extension(home, "anthropic.claude-code", "extension.js")
    codex_home = Path(env.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    observations = {
        "codex_generic_ipc_socket_present": socket_exists(codex_home / "ipc/ipc.sock"),
        "codex_app_server_daemon_socket_present": socket_exists(
            codex_home / "app-server-control/app-server-control.sock"),
        "claude_base_url_override_configured": base_url_override(home, env),
    }
    if platform == "linux":
        config = Path(env.get("XDG_CONFIG_HOME", str(home / ".config")))
        observations["claude_desktop_tail_gate"] = cached_claude_tail_gate(config / "Claude/fcache")
    binding = {
        "platform": platform,
        "host_fingerprint": host_fingerprint(home, platform, codex_home),
        "implementation_fingerprint": implementation_fingerprint(),
        "client_versions": {name: components[name]["version"] for name in NATIVE_COMPONENTS},
        "codex_ipc_versions": dict(IPC_VERSIONS),
    }
    acceptance = inspect_acceptance(acceptance_path(home, env), binding)
    # These are implementation gates, not configurable feature flags. A receipt,
    # present socket, or enabled cached feature cannot create a missing adapter.
    capability_blockers = [
        {"code": "claude_native_follow",
         "reason": "The native Claude follower capability is not enabled in this implementation.",
         "next": "Validate all three native views with one successful Claude owner."},
        {"code": "native_workflow_acceptance",
         "reason": "Native dispatch/revision/recovery acceptance is not enabled in this implementation.",
         "next": "Use the version-pinned adapters to validate native views throughout both full acceptance loops."},
        {"code": "bidirectional_acceptance",
         "reason": "Bidirectional native acceptance is not enabled in this implementation.",
         "next": "Validate both implemented owner adapters with same-session native revision and recovery."},
    ]
    capabilities = {name: IMPLEMENTATION_CAPABILITIES.get(name) is True
                    for name in (item["code"] for item in capability_blockers)}
    runtime_blockers = [item for item in capability_blockers if not capabilities[item["code"]]]
    if platform != "linux":
        runtime_blockers.append({"code": "runtime_platform", "reason": "A complete runtime adapter for this OS is not implemented.",
                                 "next": "Implement and validate this platform before enabling its runtime."})
    missing = [name for name in NATIVE_COMPONENTS if not components[name]["installed"] or
               not isinstance(components[name]["version"], str) or not components[name]["version"]]
    if missing:
        runtime_blockers.append({"code": "native_client_inventory", "reason": "Missing native client versions: " + ", ".join(missing),
                                 "next": "Install supported native clients before full local acceptance."})
    if any(components[name]["protocol_match"] is not True for name in ("codex_desktop", "codex_vscode")):
        runtime_blockers.append({"code": "codex_native_protocol", "reason": "Installed Codex native IPC protocols are not verified as compatible.",
                                 "next": "Use supported version-pinned adapters and repeat local acceptance after updates."})
    if not acceptance["verified"]:
        runtime_blockers.append({"code": "local_acceptance", "reason": acceptance["reason"],
                                 "next": "Record both complete native acceptance directions for this host and exact installation."})
    # Cross-platform release is a separate gate. Local Linux receipts cannot
    # claim Windows/macOS validation; no speculative cross-platform receipt
    # protocol is implemented here.
    platform_blockers = [{"code": "platform_validation", "platforms": ["win32", "darwin"],
                          "reason": "Real Windows and macOS native workflow acceptance remains unverified.",
                          "next": "Validate both target OS implementations before a cross-platform release."}]
    runtime_ready = not runtime_blockers
    release_ready = runtime_ready and not platform_blockers
    return {
        "schema_version": 1, "platform": platform, "runtime_ready": runtime_ready, "release_ready": release_ready,
        "status": "ready" if release_ready else "development_ready" if runtime_ready else "blocked",
        "mode": "read_only_no_model_calls",
        "components": components, "observations": observations,
        # Retain the combined field for old report consumers while exposing the
        # two scopes explicitly to runtime and release callers.
        "blockers": runtime_blockers + platform_blockers,
        "runtime_blockers": runtime_blockers, "platform_blockers": platform_blockers,
        "implementation_capabilities": capabilities,
        "local_acceptance": {**acceptance, "binding": binding,
                             "required_directions": list(ACCEPTANCE_DIRECTIONS),
                             "required_scenarios": list(ACCEPTANCE_SCENARIOS)},
        "development_evidence": {
            "basis": "recorded_development_checks_not_a_probe_of_this_computer",
            "date": "2026-10-26", "reference": "docs/native-validation.md",
            "codex_tui": "real_desktop_owner_history_and_live_updates_observed",
            "claude_vscode": "real_tasks_tools_tests_review_reports_and_reconnection_observed",
            "claude_desktop": "real_tasks_tools_tests_review_reports_and_running_reconnection_observed",
            "claude_owner": "same_session_implementation_revision_feedback_and_foreground_stop_observed",
            "codex_owner": "same_session_revision_feedback_reconnection_and_pidfd_stop_observed",
            "linux_native_workflow_verified": True,
            "full_bridge_verified": False,
        },
        "evidence_limits": [
            "Installed versions are observations, not proof of which process is active.",
            "Feature-gate state is cached and may differ from the running client or active account.",
            "No conversation was opened, connected, resumed, or started by this check.",
            "Local receipts are checked for host/version binding and artifact integrity; doctor does not rerun or judge their recorded tests.",
            "Runtime readiness does not verify current provider quota or replace runtime connection/permission checks.",
        ],
    }
