"""Prepare a removable Linux Desktop copy with a bound read-only LocalSessions API.

The installed application and its authentication are left intact. This developer
copy uses the normal Desktop profile, so opening is refused while any original
Desktop process is alive. It is not a released native adapter or acceptance proof.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import uuid

from native_probe import Asar
from native_view import ViewError, write_private
from claude_vscode_proxy import Binding, Follower, ProtocolError


VERSION = "3.6556.1"
CHUNK = ".vite/build/index.chunk-CTo_fOxG.js"
CHUNK_HASH = "3301b0b9d340c5ca4f6e69c83edfa9f984e2258d9709414571a0c6b544c63c36"
FACTORY = 'let r=CW((async()=>{let[{createLocalSessionsApi:t},n]=await Promise.all([Promise.resolve().then((()=>require("./index.chunk-DqktEifE.js"))),Sw()]);return t({webContents:e},n,(e=>Ab.getDispatcher(e)))}));'
KIND = "claudex-bridge-claude-desktop-view"
PRELOAD = ".vite/build/mainView.js"
PRELOAD_HASH = "f28f3650f4fe3eb123d45da864b02e79fa223f249e860e6c5bd196082c8a9cba"
PRE_MAIN = ".vite/build/index.pre.js"
PRE_MAIN_HASH = "3b7cb1557fd6dcffae9f47a19ae212650095ab34228efbeed0fabb13ff07434b"


def patch_listener_ack(source, channel):
    for method, kind, native in (("onNavigate", "navigation", "Navigation_$_navigate"),
                                 ("onOnEvent", "events", "LocalSessions_$_onEvent")):
        anchor = (method + '(e){let n=(t,n)=>e(n),r=Y+"' + native +
                  '";return t.ipcRenderer.on(r,n),()=>{t.ipcRenderer.removeListener(r,n)}}')
        if source.count(anchor) != 1:
            raise ViewError("Unrecognized native listener registration")
        prefix = ('t.ipcRenderer.send(' + json.dumps(channel) + ',{kind:' + json.dumps(kind) + ',subscription:i,present:')
        replacement = (method + '(e){let n=(t,n)=>e(n),r=Y+' + json.dumps(native) +
                       ',i=Date.now().toString(36)+"-"+Math.random().toString(36).slice(2);return '
                       't.ipcRenderer.on(r,n),' + prefix + 'true}),()=>{t.ipcRenderer.removeListener(r,n);' + prefix + 'false})}}')
        source = source.replace(anchor, replacement, 1)
    return source


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rewrite_asar(source, destination, replacements):
    """Write a new archive, preserving unpacked entries and executable metadata."""
    source, destination = Path(source), Path(destination)
    archive = Asar(source)
    tree = copy.deepcopy(archive.tree)
    entries = []
    def walk(node, prefix=""):
        for name, value in node["files"].items():
            path = prefix + name
            if "files" in value:
                walk(value, path + "/")
            elif "offset" in value and not value.get("unpacked"):
                entries.append((path, value))
    walk(tree)
    if not set(replacements).issubset({path for path, _ in entries}):
        raise ViewError("An ASAR patch target is not a packed regular file")
    contents = []
    offset = 0
    with source.open("rb") as stream:
        for path, value in entries:
            if path in replacements:
                raw = replacements[path]
                block_size = 4 * 1024 * 1024
                value["integrity"] = {
                    "algorithm": "SHA256", "hash": hashlib.sha256(raw).hexdigest(), "blockSize": block_size,
                    "blocks": [hashlib.sha256(raw[i:i + block_size]).hexdigest()
                               for i in range(0, len(raw), block_size)],
                }
            else:
                stream.seek(archive.base + int(value["offset"]))
                raw = stream.read(value["size"])
                if len(raw) != value["size"]:
                    raise ViewError("The installed archive is incomplete")
            value["offset"], value["size"] = str(offset), len(raw)
            offset += len(raw)
            contents.append(raw)
    payload = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    aligned = (len(payload) + 4 + 3) // 4 * 4
    header = struct.pack("<II", aligned, len(payload)) + payload + b"\0" * (aligned - 4 - len(payload))
    with destination.open("xb") as stream:
        stream.write(struct.pack("<II", 4, len(header)))
        stream.write(header)
        for raw in contents:
            stream.write(raw)


def prepare(directory, project, binding_path, resources=None):
    if sys.platform != "linux":
        raise ViewError("This Desktop copy adapter is currently implemented for Linux only")
    directory = Path(directory).expanduser().resolve()
    project = Path(project).expanduser().resolve()
    original = Path(resources or "/usr/lib/claude-desktop/resources").expanduser().resolve()
    binding_path = Path(binding_path).expanduser().resolve()
    if directory.exists() or not directory.parent.is_dir():
        raise ViewError("Choose a new output directory with an existing parent")
    if not project.is_dir():
        raise ViewError("Project directory does not exist")
    try:
        binding = Binding.load(str(binding_path), check_ready_file=False)
        if binding.history != "replay" or binding.source_format != "transcript":
            raise ViewError("Desktop follower requires a replay transcript binding")
        observer = Follower(binding)
        observer.close()
    except ProtocolError as exc:
        raise ViewError(str(exc)) from None
    archive = Asar(original / "app.asar")
    package = json.loads(archive.read("package.json"))
    chunk = archive.read(CHUNK)
    preload = archive.read(PRELOAD)
    pre_main = archive.read(PRE_MAIN)
    if (package.get("name") != "@ant/desktop" or package.get("version") != VERSION
            or hashlib.sha256(chunk.encode()).hexdigest() != CHUNK_HASH or chunk.count(FACTORY) != 1
            or hashlib.sha256(preload.encode()).hexdigest() != PRELOAD_HASH
            or hashlib.sha256(pre_main.encode()).hexdigest() != PRE_MAIN_HASH):
        raise ViewError("Unsupported Desktop build; refusing to patch an unverified factory")
    executable = original.parent / "claude-desktop"
    if not executable.is_file():
        raise ViewError("Expected the installed Linux Desktop executable")
    scripts = Path(__file__).resolve().parent
    files = ("claude_desktop_loader.js", "claude_desktop_follower.js",
             "claude_desktop_source.py", "claude_vscode_proxy.py", "claude_owner.py")
    for name in files:
        if not (scripts / name).is_file():
            raise ViewError("Desktop follower component is missing: " + name)
    adapter = directory / "adapter"
    sealed_binding = adapter / "binding.json"
    loader = adapter / "claude_desktop_loader.js"
    channel = "claudex-bridge:follower:" + str(uuid.uuid4())
    preload = patch_listener_ack(preload, channel)
    bootstrap = ('require(' + json.dumps(str(loader)) + ').installListenerGate(require("electron").ipcMain,'
                 + json.dumps(channel) + ');\n')
    replacement = ('let r=CW((async()=>{return require(' + json.dumps(str(loader)) + ').create({'
                   'bindingPath:' + json.dumps(str(sealed_binding)) + ',python:' + json.dumps(sys.executable) + ','
                   'sourceScript:' + json.dumps(str(adapter / "claude_desktop_source.py")) + ','
                   'webContents:e,onEvent:event=>{const d=Ab.getDispatcher(e);if(!d)throw Error("Native event dispatcher unavailable");d.dispatchOnEvent(event)},'
                   'navigate:route=>{const d=xb.getDispatcher(e);if(!d)throw Error("Native navigation dispatcher unavailable");d.dispatchNavigate(route)}})}));')
    directory.mkdir(mode=0o700)
    try:
        adapter.mkdir(mode=0o700)
        for name in files:
            shutil.copyfile(scripts / name, adapter / name)
            (adapter / name).chmod(0o600)
        # Seal the binding already validated above. Rereading its pathname here
        # could switch sessions after validation while metadata kept the old ID.
        data = {
            "schema": 1, "session_id": binding.session_id, "source": str(binding.source),
            "source_format": binding.source_format, "claude_code_version": binding.claude_code_version,
            "history": binding.history, "desktop_version": VERSION, "cwd": str(project),
        }
        if binding.owner_state_dir is not None:
            data["owner_state_dir"] = str(binding.owner_state_dir)
        write_private(sealed_binding, data)
        app = directory / "app"
        app.mkdir(mode=0o700)
        for item in original.parent.iterdir():
            if item.name == "resources":
                continue
            if item.name == executable.name:
                shutil.copyfile(item, app / item.name)
                (app / item.name).chmod(0o700)
            else:
                (app / item.name).symlink_to(item, target_is_directory=item.is_dir())
        (app / "resources").mkdir(mode=0o700)
        for item in original.iterdir():
            if item.name != "app.asar":
                (app / "resources" / item.name).symlink_to(item, target_is_directory=item.is_dir())
        patched_asar = app / "resources/app.asar"
        rewrite_asar(original / "app.asar", patched_asar, {
            CHUNK: chunk.replace(FACTORY, replacement).encode(),
            PRELOAD: preload.encode(), PRE_MAIN: (bootstrap + pre_main).encode(),
        })
        metadata = {
            "schema": 1, "kind": KIND, "desktop_version": VERSION, "session_id": binding.session_id,
            "project": str(project), "original_resources": str(original),
            "original_asar_sha256": sha(original / "app.asar"),
            "files": {str(path.relative_to(directory)): sha(path) for path in
                      [*adapter.iterdir(), app / executable.name, patched_asar]},
            "native_integration_verified": False,
            "native_route": "/epitaxy/local_" + binding.session_id,
        }
        write_private(directory / "view.json", metadata)
    except BaseException:
        shutil.rmtree(directory)
        raise
    return {"directory": str(directory), "session_id": binding.session_id, "prepared": True,
            "execution_started": False, "native_ui_verified": False,
            "requires_closed_desktop": True, "status": "development_adapter_unverified"}


def open_view(directory):
    directory = Path(directory).expanduser().resolve()
    metadata = json.loads((directory / "view.json").read_text())
    if metadata.get("schema") != 1 or metadata.get("kind") != KIND:
        raise ViewError("Not a prepared Desktop follower")
    original = Path(metadata["original_resources"])
    if sha(original / "app.asar") != metadata["original_asar_sha256"]:
        raise ViewError("Installed Desktop changed; prepare a compatible copy again")
    for name, digest in metadata["files"].items():
        path = directory / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink() or sha(path) != digest:
            raise ViewError("Prepared Desktop component changed; refusing launch")
    # Both original and prepared applications use the existing login/profile.
    # Never run concurrent processes on that profile or stop one automatically.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            executable = (entry / "exe").resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if executable.name == "claude-desktop":
            raise ViewError("Claude Desktop is running; close it normally before opening the prepared copy")
    log = directory / "launch.log"
    with log.open("ab") as output:
        log.chmod(0o600)
        process = subprocess.Popen([str(directory / "app/claude-desktop")], stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, start_new_session=True)
    return {"open_requested": True, "pid": process.pid, "execution_started": False,
            "native_ui_verified": False, "session_id": metadata["session_id"]}
