"""folders.py: where the Save / Open boxes start (the recordings folder)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import folders  # noqa: E402


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.install = self.tmp / "install"     # stands in for the exe's folder
        self.install.mkdir()
        self.docs = self.tmp / "Docs"
        self.docs.mkdir()
        self.windows = lambda: str(self.docs)


class ValidFolderTextTests(unittest.TestCase):
    def test_absolute_paths_only(self):
        good = str(Path(tempfile.gettempdir()) / "x")
        self.assertTrue(folders.valid_folder_text(good))
        for bad in (None, "", "   ", "relative/folder", 5, ["a"], "/a\0b", "/" + "a" * 2000):
            with self.subTest(bad=bad):
                self.assertFalse(folders.valid_folder_text(bad))


class DocumentsTests(TempCase):
    def test_windows_answer_wins(self):
        self.assertEqual(folders.documents_dir(self.windows), self.docs)

    def test_no_answer_or_a_crash_falls_back_to_the_home_folder(self):
        def broken():
            raise OSError("no shell32")
        with mock.patch.object(Path, "home", return_value=self.tmp):
            for windows in (None, lambda: None, lambda: "relative", broken):
                with self.subTest(windows=windows):
                    self.assertEqual(folders.documents_dir(windows), self.tmp)   # no tmp/Documents yet
            (self.tmp / "Documents").mkdir()
            self.assertEqual(folders.documents_dir(None), self.tmp / "Documents")


class InstallerChoiceTests(TempCase):
    def write(self, text):
        (self.install / folders.INSTALL_INFO_NAME).write_text(text, encoding="utf-8")

    def test_missing_file_means_no_choice(self):
        self.assertIsNone(folders.installer_choice(self.install))

    def test_reads_the_folder_setup_wrote(self):
        picked = str(self.tmp / "Picked")
        self.write("﻿" + json.dumps({"recordings_dir": picked}))   # Setup may write a BOM
        self.assertEqual(folders.installer_choice(self.install), picked)

    def test_bad_files_are_ignored(self):
        for text in ("not json", "[]", '{"recordings_dir": "relative"}', '{"recordings_dir": 3}'):
            with self.subTest(text=text):
                self.write(text)
                self.assertIsNone(folders.installer_choice(self.install))


class RecordingsDirTests(TempCase):
    def test_default_is_documents_wackro(self):
        self.assertEqual(folders.recordings_dir({"recordings_dir": None}, self.windows, self.install),
                         self.docs / "Wackro")

    def test_setup_choice_beats_documents(self):
        picked = self.tmp / "FromSetup"
        (self.install / folders.INSTALL_INFO_NAME).write_text(json.dumps({"recordings_dir": str(picked)}))
        self.assertEqual(folders.default_recordings_dir(self.windows, self.install), picked)
        self.assertEqual(folders.recordings_dir({}, self.windows, self.install), picked)

    def test_the_users_own_pick_beats_everything(self):
        own = self.tmp / "Mine"
        (self.install / folders.INSTALL_INFO_NAME).write_text(json.dumps({"recordings_dir": str(self.tmp / "S")}))
        self.assertEqual(folders.recordings_dir({"recordings_dir": str(own)}, self.windows, self.install), own)

    def test_start_dir_creates_the_folder(self):
        start = folders.dialog_start_dir({}, self.windows, self.install)
        self.assertEqual(start, str(self.docs / "Wackro"))
        self.assertTrue((self.docs / "Wackro").is_dir())

    def test_a_folder_that_cant_be_made_falls_back_to_documents(self):
        blocker = self.tmp / "file"
        blocker.write_text("x")                     # a file where the folder should go
        start = folders.dialog_start_dir({"recordings_dir": str(blocker / "sub")}, self.windows, self.install)
        self.assertEqual(start, str(self.docs))

    def test_install_dir_from_source_is_the_project(self):
        self.assertEqual(folders.install_dir(), Path(folders.__file__).resolve().parent)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", str(self.install / "Wackro.exe")):
            self.assertEqual(folders.install_dir(), self.install.resolve())


if __name__ == "__main__":
    unittest.main()
