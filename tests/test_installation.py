import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cverify.cli import main
from cverify import __version__
from cverify.execution import Toolchain
from cverify.installation import misra_rule_texts, tool_directory, user_data_directory


class InstallationTests(unittest.TestCase):
    def test_version_does_not_require_tools_or_a_project(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["--version"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "OpenEmbeddLint " + __version__)

    def test_user_data_locations_follow_the_host(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.dict(os.environ, {"LOCALAPPDATA": directory, "XDG_DATA_HOME": directory}):
                with patch("cverify.installation.sys.platform", "win32"):
                    self.assertEqual(user_data_directory(), base / "OpenEmbeddLint")
                with patch("cverify.installation.sys.platform", "linux"):
                    self.assertEqual(user_data_directory(), base / "openembeddlint")
                with patch("cverify.installation.sys.platform", "darwin"), patch("cverify.installation.Path.home", return_value=base):
                    self.assertEqual(user_data_directory(), base / "Library/Application Support/OpenEmbeddLint")

    def test_existing_checkout_tools_and_an_explicit_parent_are_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "tools/platforms/linux-x86_64"
            bundled.mkdir(parents=True)
            with patch.dict(os.environ, {"OPENEMBEDDLINT_TOOLS_DIR": ""}):
                self.assertEqual(tool_directory(root, "linux-x86_64"), bundled)
            with patch.dict(os.environ, {"OPENEMBEDDLINT_TOOLS_DIR": str(root / "separate-tools")}):
                toolchain = Toolchain(root, "linux-x86_64")
                self.assertEqual(toolchain.root, (root / "separate-tools/linux-x86_64").resolve())

    def test_installed_package_uses_user_tools_without_creating_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"OPENEMBEDDLINT_TOOLS_DIR": ""}), patch("cverify.installation.user_data_directory", return_value=root / "data"):
                self.assertEqual(tool_directory(root, "linux-x86_64"), root / "data/tools/platforms/linux-x86_64")
                self.assertFalse((root / "data").exists())

    def test_relative_tools_and_explicit_missing_texts_are_rejected(self):
        with patch.dict(os.environ, {"OPENEMBEDDLINT_TOOLS_DIR": "relative"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                Toolchain(Path.cwd(), "linux-x86_64")
        with patch.dict(os.environ, {"OPENEMBEDDLINT_MISRA_RULE_TEXTS": "relative.txt"}):
            with self.assertRaisesRegex(ValueError, "absolute"):
                misra_rule_texts()

    def test_default_private_text_file_is_optional_and_discovered_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"OPENEMBEDDLINT_MISRA_RULE_TEXTS": ""}), patch("cverify.installation.user_data_directory", return_value=root):
                path = misra_rule_texts()
                self.assertFalse(path.exists())
                path.write_text("local authorized text", encoding="utf-8")
                self.assertEqual(misra_rule_texts(), path)


if __name__ == "__main__":
    unittest.main()
