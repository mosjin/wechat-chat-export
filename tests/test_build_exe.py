# -*- coding: utf-8 -*-
"""build_exe.py 单测：平台守卫、命令构造与工作流 YAML 逐项对齐、
zip 组装清单、失败即停编排。全部可在非 Windows 环境运行。"""

from __future__ import annotations

import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import build_exe

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "build-windows.yml"

FAKE_PY = "python"


def read_workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


class PlatformGuardTests(unittest.TestCase):
    def test_windows_accepted(self):
        self.assertTrue(build_exe.is_windows_platform("Windows"))
        self.assertEqual(build_exe.require_windows("Windows"), "Windows")

    def test_non_windows_rejected_with_clear_message(self):
        for system_name in ("Linux", "Darwin", ""):
            with self.subTest(system_name=system_name):
                self.assertFalse(build_exe.is_windows_platform(system_name))
                with self.assertRaises(build_exe.BuildError) as caught:
                    build_exe.require_windows(system_name)
                message = str(caught.exception)
                self.assertIn("Windows 10/11", message)
                self.assertIn("Python 3.12", message)

    def test_main_returns_1_on_non_windows_without_running_steps(self):
        calls: list = []

        def runner(command):  # pragma: no cover - 不应被调用
            calls.append(command)
            return 0

        exit_code = build_exe.main(system_name="Linux", runner=runner)
        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, [])


class CommandConstructionTests(unittest.TestCase):
    def setUp(self):
        self.workflow = read_workflow()

    def test_collect_all_matches_workflow(self):
        yaml_packages = re.findall(r"--collect-all\s+(\S+)", self.workflow)
        self.assertEqual(list(build_exe.COLLECT_ALL_PACKAGES), yaml_packages)

    def test_pyinstaller_command_matches_workflow(self):
        command = build_exe.pyinstaller_command(FAKE_PY)
        self.assertEqual(command[:3], [FAKE_PY, "-m", "PyInstaller"])
        for flag in ("--noconfirm", "--clean", "--onefile", "--windowed"):
            self.assertIn(flag, command)
            self.assertIn(flag, self.workflow)
        name_index = command.index("--name")
        self.assertEqual(command[name_index + 1], build_exe.APP_NAME)
        self.assertIn(f'--name "{build_exe.APP_NAME}"', self.workflow)
        icon_index = command.index("--icon")
        self.assertEqual(command[icon_index + 1], build_exe.ICON_PATH)
        self.assertIn(f'--icon "{build_exe.ICON_PATH}"', self.workflow)
        hidden_index = command.index("--hidden-import")
        self.assertEqual(command[hidden_index + 1], "win32timezone")
        self.assertIn("--hidden-import win32timezone", self.workflow)
        binary_index = command.index("--add-binary")
        self.assertEqual(command[binary_index + 1], build_exe.ADD_BINARY_SPEC)
        self.assertIn(
            f'--add-binary "{build_exe.ADD_BINARY_SPEC}"', self.workflow
        )
        self.assertEqual(command[-1], build_exe.ENTRY_POINT)

    def test_pip_commands_match_workflow(self):
        commands = build_exe.pip_install_commands(FAKE_PY)
        self.assertEqual(len(commands), 3)
        joined = [" ".join(cmd) for cmd in commands]
        self.assertIn("pip install --upgrade pip", joined[0])
        self.assertIn("pip install pyinstaller", joined[1])
        self.assertIn("pip install -r requirements.txt", joined[2])
        for snippet in (
            "python -m pip install --upgrade pip",
            "python -m pip install pyinstaller",
            "python -m pip install -r requirements.txt",
        ):
            self.assertIn(snippet, self.workflow)

    def test_py_compile_list_matches_workflow(self):
        match = re.search(r"python -m py_compile ([^\n]+)", self.workflow)
        self.assertIsNotNone(match)
        yaml_files = match.group(1).split()
        self.assertEqual(list(build_exe.PY_COMPILE_FILES), yaml_files)
        self.assertEqual(
            build_exe.py_compile_command(FAKE_PY),
            [FAKE_PY, "-m", "py_compile", *yaml_files],
        )

    def test_unit_test_command_matches_workflow(self):
        self.assertIn(
            'python -m unittest discover -s tests -p "test_*.py"',
            self.workflow,
        )
        self.assertEqual(
            build_exe.unit_test_command(FAKE_PY),
            [FAKE_PY, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_*.py"],
        )

    def test_rust_silk_constants_match_workflow(self):
        self.assertIn(build_exe.RUST_SILK_URL, self.workflow)
        self.assertIn(build_exe.RUST_SILK_SHA256, self.workflow)

    def test_wechatauto_verify_script_key_assertions(self):
        script = build_exe.WECHATAUTO_VERIFY_SCRIPT
        for snippet in (
            "wechatauto-replica",
            "WECHATAUTO_KEYS_DIR",
            "tempfile.gettempdir()",
            "extract_master_key",
            "decrypt_image",
            "wechatauto.__version__",
        ):
            self.assertIn(snippet, script)
            self.assertIn(snippet, self.workflow)

    def test_step_order_matches_workflow(self):
        names = [step.name for step in build_exe.build_steps()]
        self.assertEqual(
            names,
            [
                "Install dependencies",
                "Verify pinned wechatauto API",
                "Download pinned rust-silk",
                "Syntax check",
                "Unit tests",
                "Build GUI EXE",
                "Prepare package",
            ],
        )
        positions = [self.workflow.index(f"name: {name}") for name in names]
        self.assertEqual(positions, sorted(positions))


class OrchestrationTests(unittest.TestCase):
    def _ctx(self, tmp: Path) -> build_exe.BuildContext:
        return build_exe.BuildContext(
            root=tmp, python_exe=FAKE_PY, runner=lambda cmd: 0
        )

    def test_failure_stops_remaining_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []

            def ok(name):
                return lambda ctx: calls.append(name)

            def boom(ctx):
                calls.append("boom")
                raise build_exe.BuildError("注入失败")

            steps = [
                build_exe.BuildStep("one", ok("one")),
                build_exe.BuildStep("two", boom),
                build_exe.BuildStep("three", ok("three")),
            ]
            failed = build_exe.run_steps(steps, self._ctx(Path(tmp)))
            self.assertEqual(failed, "two")
            self.assertEqual(calls, ["one", "boom"])

    def test_command_failure_raises_with_step_context(self):
        def failing_runner(command):
            return 3

        with self.assertRaises(build_exe.BuildError) as caught:
            build_exe.run_command(
                failing_runner, [FAKE_PY, "-m", "pip"], "Install"
            )
        self.assertIn("3", str(caught.exception))

    def test_all_steps_success_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = [
                build_exe.BuildStep("a", lambda ctx: None),
                build_exe.BuildStep("b", lambda ctx: None),
            ]
            self.assertIsNone(build_exe.run_steps(steps, self._ctx(Path(tmp))))


class PackageTests(unittest.TestCase):
    def test_zip_members_match_workflow_package(self):
        members = build_exe.package_zip_members()
        self.assertEqual(
            members,
            [
                "WeChat-Chat-Export-for-LLM.exe",
                "README.md",
                "LICENSE",
                "THIRD_PARTY_NOTICES.md",
            ],
        )
        workflow = read_workflow()
        for member in members:
            # 工作流 pwsh 里写的是 dist\\<文件>（双反斜杠字面）
            self.assertIn("dist\\\\" + member, workflow)

    def test_prepare_package_builds_expected_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            dist.mkdir()
            (dist / build_exe.EXE_NAME).write_bytes(b"exe")
            for name in build_exe.PACKAGE_FILES:
                (root / name).write_text(name, encoding="utf-8")
            zip_path = build_exe.prepare_package(root)
            self.assertEqual(zip_path.name, build_exe.ZIP_NAME)
            with zipfile.ZipFile(zip_path) as zipped:
                self.assertEqual(
                    zipped.namelist(), build_exe.package_zip_members()
                )

    def test_prepare_package_missing_exe_fails_without_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dist").mkdir()
            for name in build_exe.PACKAGE_FILES:
                (root / name).write_text(name, encoding="utf-8")
            with self.assertRaises(build_exe.BuildError):
                build_exe.prepare_package(root)
            self.assertFalse((root / "dist" / build_exe.ZIP_NAME).exists())

    def test_sha256_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blob.bin"
            target.write_bytes(b"abc")
            good = (
                "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A"
                "9CB410FF61F20015AD"
            )
            build_exe.verify_sha256(target, good)
            with self.assertRaises(build_exe.BuildError):
                build_exe.verify_sha256(target, "0" * 64)

    def test_find_rust_silk_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            extract = Path(tmp)
            nested = extract / "sub"
            nested.mkdir()
            (nested / "rust-silk.exe").write_bytes(b"x")
            self.assertEqual(
                build_exe.find_rust_silk_exe(extract), nested / "rust-silk.exe"
            )
            empty = extract / "empty"
            empty.mkdir()
            with self.assertRaises(build_exe.BuildError):
                build_exe.find_rust_silk_exe(empty)


if __name__ == "__main__":
    unittest.main()
