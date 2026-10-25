"""Real temporary Git worktrees exercise evidence, never a user's project/model."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/claudex-bridge/scripts"
sys.path.insert(0, str(SCRIPTS))
import git_evidence as evidence


@unittest.skipUnless(shutil.which("git"), "Git is needed for worktree evidence tests")
class GitEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claudex-evidence-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.git_environment = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                                    GIT_CONFIG_NOSYSTEM="1")
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX"):
            self.git_environment.pop(name, None)
        self.git("init", "-q")
        (self.project / "tracked.txt").write_text("original\n", encoding="utf-8")
        self.commit("Initial fixture")
        self.number = 0

    def git(self, *arguments):
        command = ["git", "-C", str(self.project), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                   "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(self.root / "no-hooks"), *arguments]
        return subprocess.run(command, env=self.git_environment, capture_output=True, check=True).stdout

    def commit(self, message, allow_empty=False):
        self.git("add", "-A")
        self.git("commit", "-qm", message, *(["--allow-empty"] if allow_empty else []))

    def capture(self, baseline=None, **limits):
        self.number += 1
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                                         "GIT_CONFIG_NOSYSTEM": "1"}):
            return evidence.capture(self.project, self.root / ("evidence-" + str(self.number)),
                                    baseline=baseline, **limits)

    def verify(self, manifest):
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                                         "GIT_CONFIG_NOSYSTEM": "1"}):
            return evidence.verify_snapshot(self.project, manifest)

    def contents(self, manifest, path):
        return (Path(manifest["evidence_dir"]) / "blobs" / manifest["entries"][path]["sha256"]).read_bytes()

    def test_preserves_dirty_tracked_and_untracked_baseline_without_mutating_git(self):
        (self.project / "tracked.txt").write_text("existing staged change\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        (self.project / "tracked.txt").write_text("existing staged change\nexisting unstaged change\n", encoding="utf-8")
        (self.project / "user-notes.txt").write_text("pre-existing untracked notes\n", encoding="utf-8")
        index_before = (self.project / ".git/index").read_bytes()
        head_before = self.git("rev-parse", "HEAD")
        baseline = self.capture()
        self.assertTrue(baseline["complete"], baseline["issues"])
        self.assertEqual(self.contents(baseline, "tracked.txt"), b"existing staged change\nexisting unstaged change\n")
        self.assertEqual(self.contents(baseline, "user-notes.txt"), b"pre-existing untracked notes\n")
        self.assertEqual((self.project / ".git/index").read_bytes(), index_before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head_before)
        self.assertTrue(self.verify(baseline)["matches"])
        self.assertEqual((self.project / ".git/index").read_bytes(), index_before)

    def test_executor_commits_do_not_hide_net_changes_relative_to_baseline(self):
        (self.project / "tracked.txt").write_text("original\nuser change\n", encoding="utf-8")
        (self.project / "notes.txt").write_text("keep me\n", encoding="utf-8")
        baseline = self.capture()
        (self.project / "tracked.txt").write_text("original\nuser change\nworker change\n", encoding="utf-8")
        (self.project / "created.txt").write_text("worker file\n", encoding="utf-8")
        self.commit("Executor committed its work and existing local changes")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        delivery = self.capture(baseline)
        report = evidence.compare(baseline, delivery)
        self.assertTrue(report["complete"], report["issues"])
        self.assertTrue(report["git_metadata_changed"])
        changes = {row["path"]: row for row in report["changes"]}
        self.assertEqual(set(changes), {"tracked.txt", "created.txt"})
        self.assertIn("+worker change\n", changes["tracked.txt"]["diff"])
        self.assertNotIn("+user change\n", changes["tracked.txt"]["diff"])
        self.assertEqual(report["delivery_snapshot_hash"], delivery["snapshot_hash"])

    def test_live_changes_and_new_files_invalidate_a_previous_delivery_hash(self):
        baseline = self.capture()
        delivery = self.capture(baseline)
        self.assertEqual(baseline["snapshot_hash"], delivery["snapshot_hash"])
        self.assertTrue(self.verify(delivery)["matches"])
        (self.project / "tracked.txt").write_text("modified after review\n", encoding="utf-8")
        check = self.verify(delivery)
        self.assertTrue(check["complete"])
        self.assertFalse(check["matches"])
        self.assertNotEqual(check["current_snapshot_hash"], delivery["snapshot_hash"])
        (self.project / "tracked.txt").write_text("original\n", encoding="utf-8")
        (self.project / "later.txt").write_text("new after review\n", encoding="utf-8")
        self.assertFalse(self.verify(delivery)["matches"])

    def test_ref_or_index_changes_alone_invalidate_a_delivery(self):
        delivery = self.capture()
        self.commit("A later empty commit", allow_empty=True)
        self.assertFalse(self.verify(delivery)["matches"])
        next_delivery = self.capture(delivery)
        (self.project / "tracked.txt").write_text("staged version\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        (self.project / "tracked.txt").write_text("original\n", encoding="utf-8")
        self.assertFalse(self.verify(next_delivery)["matches"])

    def test_verification_is_read_only_and_does_not_create_another_evidence_directory(self):
        delivery = self.capture()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        self.assertTrue(self.verify(delivery)["matches"])
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_binary_content_is_referenced_and_never_silently_dropped(self):
        (self.project / "image.bin").write_bytes(b"\0before\xff")
        baseline = self.capture()
        (self.project / "image.bin").write_bytes(b"\0after\xfe")
        delivery = self.capture(baseline)
        report = evidence.compare(baseline, delivery)
        self.assertTrue(report["complete"], report["issues"])
        row = report["changes"][0]
        self.assertEqual(row["path"], "image.bin")
        self.assertTrue(row["binary"])
        self.assertIsNone(row["diff"])
        self.assertEqual(Path(row["before"]["content_file"]).read_bytes(), b"\0before\xff")
        self.assertEqual(Path(row["after"]["content_file"]).read_bytes(), b"\0after\xfe")

    @unittest.skipUnless(os.name == "posix", "POSIX fixture symlinks")
    def test_symlink_targets_are_captured_without_reading_the_target(self):
        target = self.root / "outside-target.txt"
        target.write_text("unrelated target contents", encoding="utf-8")
        link = self.project / "link"
        link.symlink_to(target)
        baseline = self.capture()
        self.assertEqual(self.contents(baseline, "link"), os.fsencode(str(target)))
        self.assertNotIn(hashlib.sha256(target.read_bytes()).hexdigest(),
                         [path.name for path in (Path(baseline["evidence_dir"]) / "blobs").iterdir()])
        link.unlink()
        link.symlink_to("nonexistent-relative-target")
        delivery = self.capture(baseline)
        row = evidence.compare(baseline, delivery)["changes"][0]
        self.assertEqual(row["before"]["kind"], "symlink")
        self.assertEqual(row["after"]["kind"], "symlink")
        self.assertIn("+nonexistent-relative-target", row["diff"])

    @unittest.skipUnless(os.name == "posix", "POSIX fixture symlinks")
    def test_replacing_a_directory_with_a_symlink_does_not_follow_its_old_children(self):
        folder = self.project / "folder"
        folder.mkdir()
        (folder / "child.txt").write_text("original child\n", encoding="utf-8")
        self.commit("Add nested file")
        baseline = self.capture()
        shutil.rmtree(folder)
        outside = self.root / "outside-directory"
        outside.mkdir()
        (outside / "child.txt").write_text("must not be captured", encoding="utf-8")
        folder.symlink_to(outside, target_is_directory=True)
        delivery = self.capture(baseline)
        self.assertTrue(delivery["complete"], delivery["issues"])
        self.assertEqual(delivery["entries"]["folder/child.txt"]["kind"], "missing")
        self.assertEqual(delivery["entries"]["folder"]["kind"], "symlink")

    def test_deleted_files_and_exact_renames_remain_reviewable_after_commit(self):
        (self.project / "delete.txt").write_text("delete content\n", encoding="utf-8")
        self.commit("Add deletion fixture")
        baseline = self.capture()
        (self.project / "delete.txt").unlink()
        (self.project / "tracked.txt").rename(self.project / "renamed.txt")
        self.commit("Rename and delete")
        delivery = self.capture(baseline)
        report = evidence.compare(baseline, delivery)
        self.assertTrue(report["complete"], report["issues"])
        changes = {row["path"]: row for row in report["changes"]}
        self.assertEqual(changes["renamed.txt"]["change"], "renamed")
        self.assertEqual(changes["renamed.txt"]["before_path"], "tracked.txt")
        self.assertEqual(changes["delete.txt"]["change"], "deleted")
        self.assertIn("-delete content", changes["delete.txt"]["diff"])

    def test_rename_with_edits_reports_both_paths_without_guessing(self):
        baseline = self.capture()
        (self.project / "tracked.txt").rename(self.project / "renamed.txt")
        (self.project / "renamed.txt").write_text("edited during rename\n", encoding="utf-8")
        delivery = self.capture(baseline)
        report = evidence.compare(baseline, delivery)
        self.assertTrue(report["complete"])
        self.assertEqual({(row["path"], row["change"]) for row in report["changes"]},
                         {("tracked.txt", "deleted"), ("renamed.txt", "added")})

    def test_new_ignore_rules_cannot_hide_a_baseline_untracked_change(self):
        (self.project / "notes.txt").write_text("original notes\n", encoding="utf-8")
        baseline = self.capture()
        (self.project / ".gitignore").write_text("notes.txt\n", encoding="utf-8")
        (self.project / "notes.txt").write_text("changed while ignored\n", encoding="utf-8")
        incomplete_scope = self.capture()
        report = evidence.compare(baseline, incomplete_scope)
        self.assertFalse(report["complete"])
        self.assertIn("baseline_paths_not_observed", [item["code"] for item in report["issues"]])
        delivery = self.capture(baseline)
        report = evidence.compare(baseline, delivery)
        self.assertTrue(report["complete"], report["issues"])
        self.assertIn("notes.txt", [row["path"] for row in report["changes"]])
        (self.project / "notes.txt").write_text("another ignored change\n", encoding="utf-8")
        self.assertFalse(self.verify(delivery)["matches"])

    def test_successive_delivery_scope_keeps_new_files_hidden_by_later_ignore_rules(self):
        baseline = self.capture()
        (self.project / "new.txt").write_text("first delivery\n", encoding="utf-8")
        first = self.capture(baseline)
        (self.project / ".gitignore").write_text("new.txt\n", encoding="utf-8")
        (self.project / "new.txt").write_text("revised delivery\n", encoding="utf-8")
        second = self.capture(first)
        report = evidence.compare(baseline, second)
        self.assertTrue(report["complete"])
        self.assertEqual(self.contents(second, "new.txt"), b"revised delivery\n")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_mode_only_changes_and_missing_final_newlines_are_explicit(self):
        baseline = self.capture()
        (self.project / "tracked.txt").chmod(0o755)
        mode_delivery = self.capture(baseline)
        mode_change = evidence.compare(baseline, mode_delivery)["changes"][0]
        self.assertNotEqual(mode_change["before"]["mode"], mode_change["after"]["mode"])
        (self.project / "tracked.txt").write_bytes(b"original")
        delivery = self.capture(mode_delivery)
        row = evidence.compare(mode_delivery, delivery)["changes"][0]
        self.assertTrue(row["before_ends_with_newline"])
        self.assertFalse(row["after_ends_with_newline"])
        self.assertIn("\\ No newline at end of file", row["diff"])

    def test_size_count_and_diff_limits_never_produce_complete_review_evidence(self):
        baseline = self.capture()
        for limits in ({"max_file_bytes": 2}, {"max_total_bytes": 2}):
            with self.subTest(limits=limits):
                limited = self.capture(baseline, **limits)
                self.assertFalse(limited["complete"])
                self.assertFalse(evidence.compare(baseline, limited)["complete"])
                self.assertFalse(self.verify(limited)["matches"])
        (self.project / "extra.txt").write_text("extra\n", encoding="utf-8")
        limited = self.capture(baseline, max_files=1)
        self.assertFalse(limited["complete"])
        self.assertIn("file_count_limit", [item["code"] for item in limited["issues"]])
        full = self.capture(baseline)
        limited_diff = evidence.compare(baseline, full, max_diff_bytes=1)
        self.assertFalse(limited_diff["complete"])
        self.assertFalse(limited_diff["changes"][0]["diff_complete"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX special files")
    def test_tracked_path_replaced_by_special_file_is_incomplete_without_opening_it(self):
        # Git does not enumerate newly created untracked FIFOs. A tracked or
        # baseline path changing into one must still be observed and rejected.
        (self.project / "tracked.txt").unlink()
        os.mkfifo(self.project / "tracked.txt")
        manifest = self.capture()
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["entries"]["tracked.txt"]["kind"], "unavailable")
        self.assertFalse(self.verify(manifest)["matches"])

    def test_file_changes_during_capture_make_the_snapshot_incomplete(self):
        actual = evidence._git_state
        calls = 0

        def changing_worktree(project):
            nonlocal calls
            result = actual(project)
            calls += 1
            if calls == 2:
                (self.project / "tracked.txt").write_text("changed during capture\n", encoding="utf-8")
            return result

        with mock.patch.object(evidence, "_git_state", side_effect=changing_worktree):
            manifest = self.capture()
        self.assertFalse(manifest["complete"])
        self.assertIn("path_changed_during_capture", [item["code"] for item in manifest["issues"]])

    def test_manifest_blob_and_git_evidence_tampering_are_rejected(self):
        baseline = self.capture()
        altered = copy.deepcopy(baseline)
        altered["entries"]["tracked.txt"]["mode"] ^= 1
        with self.assertRaisesRegex(evidence.EvidenceError, "hash changed"):
            self.verify(altered)
        blob = Path(baseline["evidence_dir"]) / "blobs" / baseline["entries"]["tracked.txt"]["sha256"]
        blob.write_bytes(b"tampered\n")
        self.assertFalse(self.verify(baseline)["matches"])
        self.assertFalse(evidence.compare(baseline, baseline)["complete"])
        clean = self.capture()
        (Path(clean["evidence_dir"]) / "git/index.stage").write_bytes(b"changed evidence")
        self.assertFalse(self.verify(clean)["matches"])

    def test_evidence_destination_cannot_be_inside_project_or_overwrite_an_existing_directory(self):
        with self.assertRaisesRegex(evidence.EvidenceError, "outside the project"):
            evidence.capture(self.project, self.project / "evidence")
        self.assertFalse((self.project / "evidence").exists())
        existing = self.root / "keep"
        existing.mkdir()
        (existing / "user.txt").write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(evidence.EvidenceError, "new evidence directory"):
            evidence.capture(self.project, existing)
        self.assertEqual((existing / "user.txt").read_text(), "preserve")

    def test_unborn_repository_and_unusual_filenames_are_supported(self):
        unborn = self.root / "unborn"
        unborn.mkdir()
        subprocess.run(["git", "-C", str(unborn), "init", "-q"], env=self.git_environment, check=True)
        (unborn / "new file.txt").write_text("new\n", encoding="utf-8")
        manifest = evidence.capture(unborn, self.root / "unborn-evidence")
        self.assertTrue(manifest["complete"])
        self.assertIsNone(manifest["git"]["head"])
        self.assertIn("new file.txt", manifest["entries"])
        if os.name == "posix":
            name = "newline\nand\ttab.txt"
            (self.project / name).write_text("safe path bytes\n", encoding="utf-8")
            unusual = self.capture()
            self.assertEqual(self.contents(unusual, name), b"safe path bytes\n")

    def test_inherited_git_environment_cannot_redirect_inspection(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "not-this-repo"),
                                         "GIT_WORK_TREE": str(self.root / "not-this-worktree"),
                                         "GIT_INDEX_FILE": str(self.root / "not-this-index")}):
            manifest = self.capture()
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["project"], str(self.project))
        self.assertFalse((self.root / "not-this-index").exists())


if __name__ == "__main__":
    unittest.main()
