from __future__ import annotations

import sys
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import manage_tools


class PruneTests(unittest.TestCase):
    def test_versioned_real_binary_survives_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "tools"
            root = tools / "platforms" / "windows-x86_64"
            (root / "bin").mkdir(parents=True)
            (root / ".cverify-toolchain").write_text("{}", encoding="utf-8")
            for name in ("clang-tidy", "clang-tidy-23", "opt"):
                (root / "bin" / name).write_bytes(b"fixture")
            with mock.patch.object(manage_tools, "TOOLS", tools):
                manage_tools.prune(["windows-x86_64"])
            self.assertTrue((root / "bin" / "clang-tidy").exists())
            self.assertTrue((root / "bin" / "clang-tidy-23").exists())
            self.assertFalse((root / "bin" / "opt").exists())
    def test_windows_foreign_create_keeps_subcommand_and_virtual_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "tools"
            executable = tools / "bootstrap" / "micromamba-linux-64" / "micromamba"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
            with mock.patch.object(manage_tools, "TOOLS", tools), \
                    mock.patch.object(manage_tools, "platform_key", return_value="windows-x86_64"), \
                    mock.patch.object(manage_tools, "wsl_path", return_value="/mnt/c/private"), \
                    mock.patch.object(manage_tools, "relocate"), \
                    mock.patch.object(manage_tools.subprocess, "run") as run:
                manage_tools.create(["linux-x86_64"])
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["C:/Windows/System32/wsl.exe", "--", "env"])
            self.assertIn("CONDA_OVERRIDE_GLIBC=2.17", command)
            self.assertIn("CONDA_OVERRIDE_LINUX=5.10", command)
            self.assertIn("create", command)
            self.assertGreater(command.index("create"), command.index("/mnt/c/private"))
            explicit = (tools / "bootstrap/linux-x86_64.explicit.txt").read_text(encoding="utf-8")
            manifest = json.loads((manage_tools.SKILL_DIR / "tools/manifest.json").read_text(encoding="utf-8"))
            packages = manifest["platforms"]["linux-x86_64"]["packages"]
            self.assertEqual(explicit, "@EXPLICIT\n" + "".join(package["url"] + "#" + package["sha256"] + "\n" for package in packages))

    def test_native_install_reuses_existing_tools_without_downloading(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            (tools / "platforms/linux-x86_64").mkdir(parents=True)
            with mock.patch.object(manage_tools, "TOOLS", tools), mock.patch.object(manage_tools, "platform_key", return_value="linux-x86_64"), mock.patch.object(manage_tools, "bootstrap") as bootstrap, mock.patch.object(manage_tools, "create") as create, mock.patch.object(manage_tools, "verify") as verify:
                manage_tools.install(["linux-x86_64"])
            bootstrap.assert_not_called()
            create.assert_not_called()
            verify.assert_called_once_with(["linux-x86_64"], False)

    def test_install_rejects_a_foreign_target_before_downloading(self):
        with mock.patch.object(manage_tools, "platform_key", return_value="linux-x86_64"), mock.patch.object(manage_tools, "bootstrap") as bootstrap:
            with self.assertRaisesRegex(RuntimeError, "native platform"):
                manage_tools.install(["macos-arm64"])
            bootstrap.assert_not_called()

    def test_windows_install_validates_hash_and_archive_paths(self):
        manifest = json.loads((manage_tools.SKILL_DIR / "tools/manifest.json").read_text(encoding="utf-8"))
        package = next(item for item in manifest["platforms"]["windows-x86_64"]["packages"] if item["name"] == "llvm-mingw")
        for name, digest, expected in [("host/bin/clang.exe", package["sha256"], None), ("../escaped-marker", package["sha256"], "escaping path"), ("host/bin/clang.exe", "wrong", "checksum mismatch")]:
            with self.subTest(name=name, digest=digest), tempfile.TemporaryDirectory() as directory:
                tools = Path(directory)
                prefix = tools / "platforms/windows-x86_64"
                prefix.mkdir(parents=True)
                archive = tools / "bootstrap" / Path(package["url"]).name
                archive.parent.mkdir()
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr(name, b"fixture")
                with mock.patch.object(manage_tools, "TOOLS", tools), mock.patch.object(manage_tools, "platform_key", return_value="windows-x86_64"), mock.patch.object(manage_tools, "sha256", return_value=digest), mock.patch.object(manage_tools, "verify") as verify:
                    if expected:
                        with self.assertRaisesRegex(RuntimeError, expected):
                            manage_tools.install(["windows-x86_64"])
                        verify.assert_not_called()
                    else:
                        manage_tools.install(["windows-x86_64"])
                        self.assertTrue((prefix / "host/bin/clang.exe").is_file())
                        verify.assert_called_once_with(["windows-x86_64"], False)
                self.assertFalse(list(tools.rglob("escaped-marker")))


if __name__ == "__main__":
    unittest.main()
