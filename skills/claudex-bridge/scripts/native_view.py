"""Prepare an isolated official VS Code view without changing the user's profile.

claudeProcessWrapper is machine-scoped in the official extension. Workspace
settings cannot install it. A separate --user-data-dir keeps the existing user's
extension settings and active conversations intact. This is a development
attachment helper, not a second model executor or a production bridge installer.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


SCHEMA = 1
KIND = "claudex-bridge-claude-native-view"


class ViewError(RuntimeError):
    pass


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ViewError("Expected a JSON object")
    return value


def write_private(path, value):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def prepare(directory, project, binding_path, extension_path, code=None):
    from claude_vscode_proxy import Binding, Follower, ProtocolError, SUPPORTED_VERSION

    if sys.platform == "win32":
        raise ViewError("The executable Python wrapper launcher is not implemented for Windows")
    directory = Path(directory).expanduser().resolve()
    project = Path(project).expanduser().resolve()
    binding_path = Path(binding_path).expanduser().resolve()
    extension = Path(extension_path).expanduser().resolve()
    if directory.exists():
        raise ViewError("Output directory already exists; use open-claude-view for an existing prepared view")
    if not project.is_dir():
        raise ViewError("Project directory does not exist")
    try:
        binding = Binding.load(str(binding_path))
        follower = Follower(binding)
        follower.close()
    except ProtocolError as exc:
        raise ViewError(str(exc)) from None
    package = read_object(extension / "package.json")
    if (package.get("publisher", "").lower() != "anthropic" or
            package.get("name") != "claude-code" or package.get("version") != SUPPORTED_VERSION):
        raise ViewError("Use the supported official Claude Code extension " + SUPPORTED_VERSION)
    executable = shutil.which(code or "code")
    if not executable:
        raise ViewError("VS Code CLI was not found; supply --code with its executable path")
    proxy = Path(__file__).with_name("claude_vscode_proxy.py").resolve()
    if not proxy.is_file():
        raise ViewError("Claude VS Code adapter is missing")
    # All preflight checks precede creating the prepared view. No credentials,
    # provider configuration or current VS Code profile are copied.
    directory.mkdir(parents=True, mode=0o700)
    profile = directory / "profile"
    user_settings = profile / "User"
    user_settings.mkdir(parents=True, mode=0o700)
    extensions = directory / "extensions"
    extensions.mkdir(mode=0o700)
    (extensions / extension.name).symlink_to(extension, target_is_directory=True)
    launcher = directory / "claude-view-wrapper"
    # Python repr is source-code quoting; no user data is interpolated into shell.
    # The extension passes the original CLI as argv[1]; preserve all those args.
    launcher.write_text(
        "#!" + sys.executable + "\n"
        "import os, runpy, sys\n"
        "sys.dont_write_bytecode = True\n"
        "os.environ['CLAUDEX_BRIDGE_CLAUDE_BINDING'] = " + repr(str(binding_path)) + "\n"
        "sys.path.insert(0, " + repr(str(proxy.parent)) + ")\n"
        "sys.argv[0] = " + repr(str(proxy)) + "\n"
        "runpy.run_path(" + repr(str(proxy)) + ", run_name='__main__')\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    write_private(user_settings / "settings.json", {
        "claudeCode.claudeProcessWrapper": str(launcher),
        "claudeCode.preferredLocation": "panel",
        # The follower has no auth endpoint or model process. The official
        # extension supports externally managed auth without its login prompt.
        "claudeCode.disableLoginPrompt": True,
        "extensions.autoUpdate": False,
        "extensions.autoCheckUpdates": False,
    })
    metadata = {
        "schema": SCHEMA, "kind": KIND, "session_id": binding.session_id,
        "project": str(project), "binding": str(binding_path),
        "binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
        "extension": str(extension), "extension_version": package["version"],
        "proxy": str(proxy), "code": executable,
    }
    write_private(directory / "view.json", metadata)
    return describe(directory, metadata)


def load(directory):
    directory = Path(directory).expanduser().resolve()
    metadata = read_object(directory / "view.json")
    if metadata.get("schema") != SCHEMA or metadata.get("kind") != KIND:
        raise ViewError("This is not a prepared Claudex Bridge native view")
    session_id = metadata.get("session_id")
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ViewError("Invalid saved session ID") from None
    binding = Path(metadata["binding"])
    if hashlib.sha256(binding.read_bytes()).hexdigest() != metadata["binding_sha256"]:
        raise ViewError("Binding changed after preparation; prepare a new view instead of switching its session")
    return directory, metadata


def describe(directory, metadata):
    return {
        "session_id": metadata["session_id"], "directory": str(directory),
        "profile": str(directory / "profile"), "project": metadata["project"],
        "mode": "read_only_native_follower", "execution_started": False,
        "native_ui_verified": False,
        "note": "Opening requests the official panel; only observing the panel can verify native rendering.",
    }


def open_view(directory):
    directory, metadata = load(directory)
    base = [metadata["code"], "--user-data-dir", str(directory / "profile"),
            "--extensions-dir", str(directory / "extensions")]
    def invoke(arguments):
        try:
            return subprocess.run(base + arguments, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise ViewError("VS Code launch timed out; inspect the existing test window before retrying") from None

    launch = invoke(["--new-window", metadata["project"]])
    if launch.returncode:
        raise ViewError("VS Code could not open the isolated view: " + launch.stderr[-1000:])
    # This official URI navigates to the exact existing session. The wrapper also
    # rejects any different/unknown session instead of launching its supplied CLI.
    uri = "vscode://anthropic.claude-code/open?session=" + metadata["session_id"]
    opened = invoke(["--open-url", uri])
    if opened.returncode:
        raise ViewError("VS Code did not accept the session URI: " + opened.stderr[-1000:])
    result = describe(directory, metadata)
    result["open_requested"] = True
    return result
