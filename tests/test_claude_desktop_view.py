"""Archive integrity, isolated preparation and refusal to disturb a live client."""
import hashlib
import contextlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))
import claude_desktop_view as desktop


def archive(path, files):
    tree = {"files": {}}
    contents = []
    offset = 0
    for name, raw in files.items():
        node = tree
        parts = name.split("/")
        for part in parts[:-1]:
            node = node["files"].setdefault(part, {"files": {}})
        node["files"][parts[-1]] = {"size": len(raw), "offset": str(offset)}
        offset += len(raw)
        contents.append(raw)
    raw = json.dumps(tree).encode()
    aligned = (len(raw) + 7) // 4 * 4
    header = struct.pack("<II", aligned, len(raw)) + raw + b"\0" * (aligned - 4 - len(raw))
    path.write_bytes(struct.pack("<II", 4, len(header)) + header + b"".join(contents))


class DesktopViewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_archive_rewrite_preserves_other_files_and_updates_integrity(self):
        source, dest = self.root / "original.asar", self.root / "patched.asar"
        archive(source, {"package.json": b'{"version":"fixture"}', "nested/script.js": b"original"})
        before = source.read_bytes()
        replacement = b"a replacement of another length"
        desktop.rewrite_asar(source, dest, {"nested/script.js": replacement})
        result = desktop.Asar(dest)
        self.assertEqual(result.read("nested/script.js"), replacement.decode())
        self.assertEqual(result.read("package.json"), '{"version":"fixture"}')
        self.assertEqual(result.node("nested/script.js")["integrity"]["hash"], hashlib.sha256(replacement).hexdigest())
        self.assertEqual(source.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            desktop.rewrite_asar(source, dest, {"nested/script.js": b"second"})
        self.assertEqual(desktop.Asar(dest).read("nested/script.js"), replacement.decode())

    def test_unknown_archive_target_creates_nothing(self):
        source, dest = self.root / "original.asar", self.root / "patched.asar"
        archive(source, {"one": b"source"})
        with self.assertRaises(desktop.ViewError):
            desktop.rewrite_asar(source, dest, {"missing": b"new"})
        self.assertFalse(dest.exists())

    def test_unsupported_platform_prepares_nothing(self):
        output = self.root / "view"
        with mock.patch.object(desktop.sys, "platform", "win32"):
            with self.assertRaisesRegex(desktop.ViewError, "Linux"):
                desktop.prepare(output, self.root, self.root / "binding.json")
        self.assertFalse(output.exists())

    def test_live_desktop_is_never_stopped_or_launched_again(self):
        original = self.root / "original"
        original.mkdir()
        (original / "app.asar").write_bytes(b"original")
        output = self.root / "view"
        output.mkdir()
        metadata = {"schema": 1, "kind": desktop.KIND, "original_resources": str(original),
                    "original_asar_sha256": desktop.sha(original / "app.asar"), "files": {}}
        (output / "view.json").write_text(json.dumps(metadata))
        process = mock.Mock()
        process.name = "1234"
        process.__truediv__ = mock.Mock(return_value=mock.Mock(resolve=mock.Mock(return_value=Path('/opt/fixture/claude-desktop'))))
        with mock.patch.object(Path, "iterdir", return_value=iter([process])), mock.patch.object(desktop.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(desktop.ViewError, "Desktop is running"):
                desktop.open_view(output)
        popen.assert_not_called()
        self.assertFalse((output / "launch.log").exists())

    def test_changed_prepared_component_refuses_before_process_launch(self):
        original = self.root / "original"
        original.mkdir()
        (original / "app.asar").write_bytes(b"original")
        output = self.root / "view"
        output.mkdir()
        code = output / "loader.js"
        code.write_bytes(b"modified")
        (output / "view.json").write_text(json.dumps({
            "schema": 1, "kind": desktop.KIND, "original_resources": str(original),
            "original_asar_sha256": desktop.sha(original / "app.asar"),
            "files": {"loader.js": hashlib.sha256(b"before").hexdigest()},
        }))
        with mock.patch.object(desktop.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(desktop.ViewError, "component changed"):
                desktop.open_view(output)
        popen.assert_not_called()


@unittest.skipUnless(sys.platform == "linux", "Desktop preparation currently targets Linux")
class DesktopPrepareTests(unittest.TestCase):
    SESSION = "00000000-0000-4000-8000-000000000001"
    OTHER = "00000000-0000-4000-8000-000000000002"
    FACTORY_START = "/* fixture factory begins */"
    FACTORY_END = "/* fixture factory ends */"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project $(not-a-command) 'quoted'"
        self.project.mkdir()
        self.install = self.root / "installed Desktop"
        self.resources = self.install / "resources"
        self.resources.mkdir(parents=True)
        self.executable = self.install / "claude-desktop"
        self.executable.write_bytes(b"#!/bin/sh\nexit 88\n")
        self.executable.chmod(0o755)
        (self.install / "library.so").write_bytes(b"untouched native dependency")
        (self.resources / "app.asar.unpacked").mkdir()
        (self.resources / "app.asar.unpacked" / "native.node").write_bytes(b"untouched unpacked dependency")
        self.chunk = self.FACTORY_START + desktop.FACTORY + self.FACTORY_END
        self.preload = (
            'const listeners={'
            'onNavigate(e){let n=(t,n)=>e(n),r=Y+"Navigation_$_navigate";return t.ipcRenderer.on(r,n),()=>{t.ipcRenderer.removeListener(r,n)}},'
            'onOnEvent(e){let n=(t,n)=>e(n),r=Y+"LocalSessions_$_onEvent";return t.ipcRenderer.on(r,n),()=>{t.ipcRenderer.removeListener(r,n)}}};'
        )
        self.pre_main = 'globalThis.fixtureBootstrap = true;\n'
        self.write_archive()
        self.source = self.root / "owner.jsonl"
        self.source.write_text(json.dumps({
            "type": "user", "sessionId": self.SESSION, "version": "2.1.295",
            "uuid": "00000000-0000-4000-8000-000000000011", "parentUuid": None,
            "message": {"role": "user", "content": "Existing task"},
        }) + "\n", encoding="utf-8")
        self.ready = self.root / "one-shot-ready.json"
        self.ready.write_text('{"previous_handshake":true}\n', encoding="utf-8")
        self.binding = self.root / "original-binding.json"
        self.binding_data = {
            "schema": 1, "session_id": self.SESSION, "source": str(self.source),
            "source_format": "transcript", "claude_code_version": "2.1.295",
            "history": "replay", "ready_file": str(self.ready),
        }
        self.binding.write_text(json.dumps(self.binding_data), encoding="utf-8")
        self.output = self.root / "new view $(not-a-command) 'quoted'"

    def write_archive(self, version=None):
        archive(self.resources / "app.asar", {
            "package.json": json.dumps({"name": "@ant/desktop", "version": version or desktop.VERSION}).encode(),
            desktop.CHUNK: self.chunk.encode(), desktop.PRELOAD: self.preload.encode(),
            desktop.PRE_MAIN: self.pre_main.encode(), "untouched/data.json": b'{"preserve":true}',
        })

    @contextlib.contextmanager
    def supported_fixture(self):
        # Only version-content fingerprints are substituted. Binding parsing,
        # source validation, patching, archive rewriting, and sealing stay real.
        with contextlib.ExitStack() as stack:
            for name, value in (("CHUNK_HASH", self.chunk), ("PRELOAD_HASH", self.preload),
                                ("PRE_MAIN_HASH", self.pre_main)):
                stack.enter_context(mock.patch.object(desktop, name, hashlib.sha256(value.encode()).hexdigest()))
            yield

    def prepare(self):
        with self.supported_fixture(), mock.patch.object(
                desktop.subprocess, "Popen", side_effect=AssertionError("prepare must not launch a process")):
            return desktop.prepare(self.output, self.project, self.binding, self.resources)

    def snapshot(self, directory):
        result = {}
        for path in sorted(directory.rglob("*")):
            name = str(path.relative_to(directory))
            if path.is_symlink():
                result[name] = ("link", os.readlink(path))
            elif path.is_dir():
                result[name] = ("directory", path.stat().st_mode & 0o777)
            else:
                result[name] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
        return result

    def test_real_prepare_only_patches_the_new_copy_and_preserves_original_install(self):
        install_before = self.snapshot(self.install)
        binding_before, ready_before = self.binding.read_bytes(), self.ready.read_bytes()
        result = self.prepare()
        self.assertEqual(self.snapshot(self.install), install_before)
        self.assertEqual(self.binding.read_bytes(), binding_before)
        self.assertEqual(self.ready.read_bytes(), ready_before)
        self.assertFalse(result["execution_started"])
        self.assertFalse(result["native_ui_verified"])
        self.assertTrue(result["requires_closed_desktop"])
        self.assertEqual(list(self.project.iterdir()), [])
        metadata = json.loads((self.output / "view.json").read_text())
        self.assertEqual(metadata["session_id"], self.SESSION)
        self.assertEqual(metadata["original_asar_sha256"], desktop.sha(self.resources / "app.asar"))
        expected = {"adapter/binding.json", "adapter/claude_desktop_loader.js", "adapter/claude_desktop_follower.js",
                    "adapter/claude_desktop_source.py", "adapter/claude_vscode_proxy.py", "app/claude-desktop",
                    "app/resources/app.asar"}
        self.assertEqual(set(metadata["files"]), expected)
        for relative, digest in metadata["files"].items():
            path = self.output / relative
            self.assertFalse(path.is_symlink(), relative)
            self.assertTrue(path.resolve().is_relative_to(self.output), relative)
            self.assertEqual(desktop.sha(path), digest)
        self.assertEqual((self.output / "app/claude-desktop").read_bytes(), self.executable.read_bytes())
        self.assertEqual((self.output / "app/library.so").resolve(), self.install / "library.so")
        self.assertEqual((self.output / "app/resources/app.asar.unpacked").resolve(), self.resources / "app.asar.unpacked")
        prepared = desktop.Asar(self.output / "app/resources/app.asar")
        self.assertEqual(prepared.read("untouched/data.json"), '{"preserve":true}')
        self.assertNotEqual(prepared.read(desktop.CHUNK), self.chunk)
        self.assertNotEqual(prepared.read(desktop.PRELOAD), self.preload)
        self.assertTrue(prepared.read(desktop.PRE_MAIN).endswith(self.pre_main))
        for target in (desktop.CHUNK, desktop.PRELOAD, desktop.PRE_MAIN):
            content = prepared.read(target).encode()
            self.assertEqual(prepared.node(target)["integrity"]["hash"], hashlib.sha256(content).hexdigest())

    def test_sealed_binding_removes_receipt_and_is_independent_of_original_after_prepare(self):
        self.prepare()
        sealed = self.output / "adapter/binding.json"
        before = sealed.read_bytes()
        data = json.loads(before)
        self.assertEqual(data, {**{key: value for key, value in self.binding_data.items() if key != "ready_file"},
                                "desktop_version": desktop.VERSION, "cwd": str(self.project)})
        self.assertEqual(sealed.stat().st_mode & 0o777, 0o600)
        # The original receipt already exists; the sealed view remains reusable.
        validated = desktop.Binding.load(str(sealed))
        self.assertIsNone(validated.ready_file)
        self.assertEqual(validated.session_id, self.SESSION)
        self.binding.write_text(json.dumps({**self.binding_data, "session_id": self.OTHER}), encoding="utf-8")
        self.assertEqual(sealed.read_bytes(), before)

    def test_sealed_binding_tamper_is_rejected_before_opening_desktop(self):
        self.prepare()
        sealed = self.output / "adapter/binding.json"
        data = json.loads(sealed.read_text())
        data["session_id"] = self.OTHER
        sealed.write_text(json.dumps(data), encoding="utf-8")
        with mock.patch.object(desktop.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(desktop.ViewError, "component changed"):
                desktop.open_view(self.output)
        popen.assert_not_called()

    def test_unknown_desktop_version_is_rejected_before_creating_output(self):
        self.write_archive(version="999.0.0")
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("No mkdir before preflight")) as mkdir:
            with self.assertRaisesRegex(desktop.ViewError, "Unsupported Desktop build"):
                self.prepare()
        mkdir.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unknown_chunk_fingerprint_is_rejected_before_creating_output(self):
        with self.supported_fixture(), mock.patch.object(desktop, "CHUNK_HASH", "0" * 64), \
                mock.patch.object(Path, "mkdir", side_effect=AssertionError("No mkdir before preflight")) as mkdir:
            with self.assertRaisesRegex(desktop.ViewError, "Unsupported Desktop build"):
                desktop.prepare(self.output, self.project, self.binding, self.resources)
        mkdir.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unknown_listener_registration_is_rejected_before_creating_output(self):
        self.preload = self.preload.replace("onNavigate(e)", "renamedNavigate(e)")
        self.write_archive()
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("No mkdir before preflight")) as mkdir:
            with self.assertRaisesRegex(desktop.ViewError, "Unrecognized native listener"):
                self.prepare()
        mkdir.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_actual_binding_protocol_error_is_wrapped_before_creating_output(self):
        self.binding_data["claude_code_version"] = "999.0.0"
        self.binding.write_text(json.dumps(self.binding_data), encoding="utf-8")
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("No mkdir before preflight")) as mkdir:
            with self.assertRaisesRegex(desktop.ViewError, "Unsupported producer version"):
                self.prepare()
        mkdir.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_actual_source_protocol_error_is_wrapped_before_creating_output(self):
        self.source.write_text("", encoding="utf-8")
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("No mkdir before preflight")) as mkdir:
            with self.assertRaisesRegex(desktop.ViewError, "Source does not prove"):
                self.prepare()
        mkdir.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_failure_after_directory_creation_removes_only_the_prepared_copy(self):
        before = self.snapshot(self.install)
        with mock.patch.object(desktop, "rewrite_asar", side_effect=OSError("fixture write failed")):
            with self.assertRaisesRegex(OSError, "fixture write failed"):
                self.prepare()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.snapshot(self.install), before)
        self.assertTrue(self.binding.is_file())
        self.assertTrue(self.source.is_file())

    def test_binding_change_during_preparation_cannot_replace_the_validated_source(self):
        copyfile = desktop.shutil.copyfile
        changed = False

        def swap_after_preflight(source, destination):
            nonlocal changed
            result = copyfile(source, destination)
            if not changed:
                changed = True
                self.binding.write_text(json.dumps({**self.binding_data, "session_id": self.OTHER,
                                                    "source": str(self.root / "unvalidated.jsonl")}), encoding="utf-8")
            return result

        with mock.patch.object(desktop.shutil, "copyfile", side_effect=swap_after_preflight):
            try:
                self.prepare()
            except desktop.ViewError:
                self.assertFalse(self.output.exists())
                return  # Detecting/rejecting a concurrent binding edit is safe too.
        sealed = json.loads((self.output / "adapter/binding.json").read_text())
        metadata = json.loads((self.output / "view.json").read_text())
        self.assertEqual(sealed["session_id"], metadata["session_id"])
        self.assertEqual(sealed["session_id"], self.SESSION)
        self.assertEqual(sealed["source"], str(self.source))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to execute the replaced Desktop factory")
    def test_replaced_factory_only_creates_reader_and_reports_missing_native_dispatchers(self):
        self.prepare()
        prepared = desktop.Asar(self.output / "app/resources/app.asar")
        chunk = prepared.read(desktop.CHUNK)
        replacement = chunk.split(self.FACTORY_START, 1)[1].split(self.FACTORY_END, 1)[0]
        loader = self.output / "adapter/claude_desktop_loader.js"
        program = '''const factory=new Function("CW","Sw","require","Ab","xb","e",DATA.factory+"return r;");
(async()=>{const results=[];for(const missing of [null,"events","navigation"]){
let creates=0,managerCalls=0,options;const requested=[],events=[],routes=[],webContents={id:17};
const api=await factory(fn=>fn(),()=>{managerCalls++;throw Error("Original manager must not load")},
name=>{requested.push(name);if(name!==DATA.loader)throw Error("Unexpected module: "+name);return {create:value=>{creates++;options=value;return {reader:true}}}},
{getDispatcher:contents=>{if(contents!==webContents)throw Error("Wrong event target");return missing==="events"?undefined:{dispatchOnEvent:event=>events.push(event)}}},
{getDispatcher:contents=>{if(contents!==webContents)throw Error("Wrong navigation target");return missing==="navigation"?undefined:{dispatchNavigate:route=>routes.push(route)}}},webContents);
let eventError=null,navigationError=null;
try{options.onEvent({type:"real-progress"})}catch(error){eventError=error.message}
try{options.navigate("/epitaxy/local_"+DATA.session)}catch(error){navigationError=error.message}
results.push({missing,creates,managerCalls,requested,reader:api.reader,binding:options.bindingPath,
sourceScript:options.sourceScript,python:options.python,sameWebContents:options.webContents===webContents,
events,routes,eventError,navigationError});}
console.log(JSON.stringify(results))})().catch(error=>{console.error(error);process.exit(1)});'''
        data = {"factory": replacement, "loader": str(loader), "session": self.SESSION}
        result = subprocess.run([shutil.which("node"), "-e", "const DATA=" + json.dumps(data) + ";\n" + program],
                                capture_output=True, text=True, check=True, timeout=10)
        records = json.loads(result.stdout)
        for row in records:
            self.assertEqual(row["creates"], 1)
            self.assertEqual(row["managerCalls"], 0)
            self.assertEqual(row["requested"], [str(loader)])
            self.assertTrue(row["reader"])
            self.assertTrue(row["sameWebContents"])
            self.assertEqual(row["binding"], str(self.output / "adapter/binding.json"))
            self.assertEqual(row["sourceScript"], str(self.output / "adapter/claude_desktop_source.py"))
            self.assertEqual(row["python"], sys.executable)
        normal, no_events, no_navigation = records
        self.assertEqual(normal["events"], [{"type": "real-progress"}])
        self.assertEqual(normal["routes"], ["/epitaxy/local_" + self.SESSION])
        self.assertIsNone(normal["eventError"])
        self.assertIsNone(normal["navigationError"])
        self.assertEqual(no_events["eventError"], "Native event dispatcher unavailable")
        self.assertEqual(no_events["events"], [])
        self.assertEqual(no_navigation["navigationError"], "Native navigation dispatcher unavailable")
        self.assertEqual(no_navigation["routes"], [])


if __name__ == "__main__":
    unittest.main()
