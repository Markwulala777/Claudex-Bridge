"""Read-only native client observations. Detection is never live validation."""
import gzip
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time
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
    # These are implementation and verification gates, not automatic feature
    # flags. The presence of a socket or an enabled cached gate cannot satisfy them.
    blockers = [
        {"code": "claude_native_follow",
         "reason": "Claude Desktop attachment, running-history handoff, and successful live model progress remain unverified.",
         "next": "Complete the native Desktop path and verify the implemented VS Code follower with one successful owner."},
        {"code": "native_workflow_acceptance",
         "reason": "Read-only native-view components do not yet establish the complete dispatch/revision/recovery workflow.",
         "next": "Use the version-pinned adapters to validate native views throughout both full acceptance loops."},
        {"code": "bidirectional_acceptance",
         "reason": "The reverse executor and full Claude/Codex acceptance loops have not passed native integration tests.",
         "next": "Implement the reverse adapter after the native-attachment gate; test same-session revision and recovery."},
        {"code": "platform_validation",
         "reason": "The complete bidirectional workflow on Linux and real Windows/macOS integration have not passed.",
         "next": "Validate each target OS before publication; offline tests do not establish native support."},
    ]
    return {
        "schema_version": 1, "platform": platform, "release_ready": False,
        "status": "blocked", "mode": "read_only_no_model_calls",
        "components": components, "observations": observations, "blockers": blockers,
        "development_evidence": {
            "basis": "recorded_development_checks_not_a_probe_of_this_computer",
            "date": "2026-10-25", "reference": "docs/native-validation.md",
            "codex_tui": "real_desktop_owner_history_and_live_updates_observed",
            "claude_vscode": "real_quota_failure_history_and_follower_handshake_observed",
            "full_bridge_verified": False,
        },
        "evidence_limits": [
            "Installed versions are observations, not proof of which process is active.",
            "Feature-gate state is cached and may differ from the running client or active account.",
            "No conversation was opened, connected, resumed, or started by this check.",
        ],
    }
