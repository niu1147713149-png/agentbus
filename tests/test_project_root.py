from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import config


class CreateProjectRoot(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for key, value in (("CONFIG", home / "config.json"), ("PROJECTS", home / "projects.json"), ("HOME", home)):
            stack.enter_context(patch.object(config, key, value))
        self.home = home

    def test_missing_directory_is_rejected_and_not_registered(self):
        missing = self.home / "does-not-exist"
        with self.assertRaises(ValueError):
            config.create_project("新项目", root=str(missing))
        self.assertNotIn("新项目", config.projects())

    def test_file_instead_of_directory_is_rejected(self):
        f = self.home / "a-file.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            config.create_project("新项目", root=str(f))
        self.assertNotIn("新项目", config.projects())

    def test_existing_directory_is_registered(self):
        d = self.home / "repo"
        d.mkdir()
        entry = config.create_project("新项目", root=str(d))
        self.assertEqual(Path(entry["root"]), d.resolve())
        self.assertIn("新项目", config.projects())


if __name__ == "__main__":
    unittest.main()
