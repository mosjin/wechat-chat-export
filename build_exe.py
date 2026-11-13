# -*- coding: utf-8 -*-
"""本地一键构建 Windows EXE（与 GitHub Actions 工作流逐项对齐）。

用法（仅 Windows 10/11 + Python 3.12）：

    python build_exe.py

步骤与 .github/workflows/build-windows.yml 一一对应：
装依赖 → wechatauto 契约校验 → 下载 rust-silk 并验 SHA256 →
py_compile → 单元测试 → PyInstaller 打包 → 组 zip。
任一步失败立即停止并报清是哪一步，不产出半成品 zip。

产物（dist/）：
    WeChat-Chat-Export-for-LLM.exe
    WeChat-Chat-Export-for-LLM-Windows.zip（exe + README + LICENSE
    + THIRD_PARTY_NOTICES.md，与 Actions artifact 结构一致）
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# ---------------------------------------------------------------------------
# 全局常量（与 build-windows.yml 对齐；改工作流时同步改这里，反之亦然）
# ---------------------------------------------------------------------------

APP_NAME = "WeChat-Chat-Export-for-LLM"
EXE_NAME = APP_NAME + ".exe"
ZIP_NAME = APP_NAME + "-Windows.zip"
ARTIFACT_NAME = APP_NAME + "-Windows"
ENTRY_POINT = "app.py"
ICON_PATH = "assets/app.ico"
DIST_DIR = "dist"
TOOLS_DIR = "tools"
REQUIREMENTS_FILE = "requirements.txt"

SUPPORTED_SYSTEM = "Windows"
SUPPORTED_PYTHON = (3, 12)

COLLECT_ALL_PACKAGES = (
    "wechatauto",
    "winsdk",
    "uiautomation",
    "comtypes",
    "imageio_ffmpeg",
    "sherpa_onnx",
    "soundfile",
)
HIDDEN_IMPORTS = ("win32timezone",)
ADD_BINARY_SPEC = "tools/rust-silk.exe;tools"

# 与工作流 "Syntax check" 步骤的 py_compile 清单一致（含本脚本自身）。
PY_COMPILE_FILES = (
    "app.py",
    "cli.py",
    "exporter_core.py",
    "merge_highlights.py",
    "preview.py",
    "wechat_data_dirs.py",
    "build_exe.py",
)

PACKAGE_FILES = ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")

RUST_SILK_VERSION = "v0.1.3"
RUST_SILK_URL = (
    "https://github.com/Wangnov/rust-silk/releases/download/"
    "v0.1.3/rust-silk-x86_64-pc-windows-msvc.zip"
)
RUST_SILK_SHA256 = (
    "44A01BC0F3EC3EC6F6044B656869B0DD7C298329D12F07C7F7A846BE72A89504"
)
RUST_SILK_EXE_NAME = "rust-silk.exe"

# 与工作流 "Verify pinned wechatauto API" 步骤的内嵌脚本逐字一致。
WECHATAUTO_VERIFY_SCRIPT = '''
import inspect
import re
from pathlib import Path

import wechatauto
from wechatauto import MediaDownloader, WeChatDB

requirement_lines = Path("requirements.txt").read_text(
    encoding="utf-8"
).splitlines()
pins = [
    line.strip()
    for line in requirement_lines
    if line.strip().startswith("wechatauto-replica")
]
assert len(pins) == 1, (
    "requirements.txt must contain exactly one "
    "wechatauto-replica pin"
)
match = re.fullmatch(
    r"wechatauto-replica==([0-9A-Za-z._+-]+)",
    pins[0],
)
assert match, (
    "wechatauto-replica must remain an exact == version pin: "
    + pins[0]
)
expected_version = match.group(1)
assert wechatauto.__version__ == expected_version, (
    expected_version,
    wechatauto.__version__,
)

for name in ("_open", "_nickname_index", "extract_master_key"):
    assert hasattr(WeChatDB, name), name
for name in (
    "_collect_templates",
    "_get_xor_key",
    "_derive_cfg_key",
    "_scan_aes_key",
    "decrypt_image",
):
    assert hasattr(MediaDownloader, name), name

stable_source = inspect.getsource(WeChatDB._stable_key_dirs)
assert "WECHATAUTO_KEYS_DIR" in stable_source

for name in ("_try_other_accounts", "_select_account_by_keys"):
    source = inspect.getsource(getattr(WeChatDB, name))
    assert "tempfile.gettempdir()" in source, name

print(
    "wechatauto-replica contract OK:",
    wechatauto.__version__,
)
'''


class BuildError(RuntimeError):
    """构建步骤失败（消息里带步骤名与原因）。"""


# ---------------------------------------------------------------------------
# 平台判断（纯函数，可单测）
# ---------------------------------------------------------------------------


def is_windows_platform(system_name: str) -> bool:
    return system_name == SUPPORTED_SYSTEM


def unsupported_platform_message(system_name: str) -> str:
    return (
        "build_exe.py 只能在 Windows 10/11 + Python 3.12 上运行，"
        f"当前平台是 {system_name or '未知'}，已退出，未做任何构建。\n"
        "云端构建路：到仓库 GitHub Actions 手动触发 "
        "Build Windows EXE 后下载 artifact（"
        + ARTIFACT_NAME
        + "）。"
    )


def require_windows(system_name: Optional[str] = None) -> str:
    """返回当前系统名；非 Windows 时抛 BuildError。"""
    name = platform.system() if system_name is None else system_name
    if not is_windows_platform(name):
        raise BuildError(unsupported_platform_message(name))
    return name


# ---------------------------------------------------------------------------
# 命令构造（纯函数，可单测；与工作流步骤逐项对应）
# ---------------------------------------------------------------------------


def pip_install_commands(python_exe: str) -> list[list[str]]:
    """工作流 "Install dependencies"：升级 pip、装 pyinstaller、装 requirements。"""
    return [
        [python_exe, "-m", "pip", "install", "--upgrade", "pip"],
        [python_exe, "-m", "pip", "install", "pyinstaller"],
        [python_exe, "-m", "pip", "install", "-r", REQUIREMENTS_FILE],
    ]


def wechatauto_verify_command(python_exe: str) -> list[str]:
    """工作流 "Verify pinned wechatauto API"：同一段校验脚本。"""
    return [python_exe, "-c", WECHATAUTO_VERIFY_SCRIPT]


def py_compile_command(python_exe: str) -> list[str]:
    """工作流 "Syntax check"。"""
    return [python_exe, "-m", "py_compile", *PY_COMPILE_FILES]


def unit_test_command(python_exe: str) -> list[str]:
    """工作流 "Unit tests"。"""
    return [
        python_exe,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-p",
        "test_*.py",
    ]


def pyinstaller_command(python_exe: str) -> list[str]:
    """工作流 "Build GUI EXE"：参数逐项一致（经 -m PyInstaller 调用）。"""
    command = [
        python_exe,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        ICON_PATH,
    ]
    for package in COLLECT_ALL_PACKAGES:
        command += ["--collect-all", package]
    for hidden in HIDDEN_IMPORTS:
        command += ["--hidden-import", hidden]
    command += ["--add-binary", ADD_BINARY_SPEC, ENTRY_POINT]
    return command


def package_zip_members() -> list[str]:
    """zip 内成员清单（与工作流 "Prepare package" 的 Compress-Archive 一致）。"""
    return [EXE_NAME, *PACKAGE_FILES]


# ---------------------------------------------------------------------------
# 执行层（薄封装）
# ---------------------------------------------------------------------------

Runner = Callable[[Sequence[str]], int]


def subprocess_runner(command: Sequence[str]) -> int:
    return subprocess.run(list(command), check=False).returncode


def run_command(runner: Runner, command: Sequence[str], step_name: str) -> None:
    print("  $ " + " ".join(str(part) for part in command))
    return_code = runner(command)
    if return_code != 0:
        raise BuildError(
            f"命令退出码 {return_code}：{' '.join(str(p) for p in command)}"
        )


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_sha256(path: Path, expected: str = RUST_SILK_SHA256) -> None:
    actual = sha256_of_file(path)
    if actual != expected:
        raise BuildError(
            f"rust-silk SHA256 mismatch. Expected {expected}, got {actual}"
        )


def find_rust_silk_exe(extract_dir: Path) -> Path:
    matches = sorted(extract_dir.rglob(RUST_SILK_EXE_NAME))
    if not matches:
        raise BuildError("rust-silk.exe was not found in the release archive")
    return matches[0]


def download_rust_silk(root: Path) -> Path:
    """工作流 "Download pinned rust-silk"：下载 → 验 SHA256 → 解压 → 落 tools/。"""
    tools_dir = root / TOOLS_DIR
    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rust-silk-") as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "rust-silk.zip"
        with urllib.request.urlopen(RUST_SILK_URL) as response:
            with archive.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        verify_sha256(archive)
        extract_dir = tmp_dir / "extract"
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(extract_dir)
        source = find_rust_silk_exe(extract_dir)
        target = tools_dir / RUST_SILK_EXE_NAME
        shutil.copyfile(source, target)
    return target


def prepare_package(root: Path) -> Path:
    """工作流 "Prepare package"：拷说明文件进 dist 并组 zip。

    zip 先写到临时文件、成功后再改名落位，失败不留半成品 zip。
    """
    dist = root / DIST_DIR
    exe = dist / EXE_NAME
    if not exe.is_file():
        raise BuildError(f"打包产物不存在：{exe}")
    for name in PACKAGE_FILES:
        source = root / name
        if not source.is_file():
            raise BuildError(f"打包所需文件不存在：{source}")
        shutil.copyfile(source, dist / name)
    zip_path = dist / ZIP_NAME
    tmp_zip = dist / (ZIP_NAME + ".tmp")
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zipped:
            for member in package_zip_members():
                zipped.write(dist / member, arcname=member)
        tmp_zip.replace(zip_path)
    except BaseException:
        tmp_zip.unlink(missing_ok=True)
        raise
    return zip_path


# ---------------------------------------------------------------------------
# 步骤编排（失败即停）
# ---------------------------------------------------------------------------


@dataclass
class BuildContext:
    root: Path
    python_exe: str
    runner: Runner


@dataclass
class BuildStep:
    name: str
    action: Callable[[BuildContext], None]


def _step_install_dependencies(ctx: BuildContext) -> None:
    for command in pip_install_commands(ctx.python_exe):
        run_command(ctx.runner, command, "Install dependencies")


def _step_verify_wechatauto(ctx: BuildContext) -> None:
    run_command(
        ctx.runner,
        wechatauto_verify_command(ctx.python_exe),
        "Verify pinned wechatauto API",
    )


def _step_download_rust_silk(ctx: BuildContext) -> None:
    target = download_rust_silk(ctx.root)
    print(f"  rust-silk 已就位：{target}")


def _step_syntax_check(ctx: BuildContext) -> None:
    run_command(ctx.runner, py_compile_command(ctx.python_exe), "Syntax check")


def _step_unit_tests(ctx: BuildContext) -> None:
    run_command(ctx.runner, unit_test_command(ctx.python_exe), "Unit tests")


def _step_build_exe(ctx: BuildContext) -> None:
    run_command(
        ctx.runner, pyinstaller_command(ctx.python_exe), "Build GUI EXE"
    )


def _step_prepare_package(ctx: BuildContext) -> None:
    zip_path = prepare_package(ctx.root)
    print(f"  发布包已生成：{zip_path}")


def build_steps() -> list[BuildStep]:
    """步骤顺序与工作流一致。"""
    return [
        BuildStep("Install dependencies", _step_install_dependencies),
        BuildStep("Verify pinned wechatauto API", _step_verify_wechatauto),
        BuildStep("Download pinned rust-silk", _step_download_rust_silk),
        BuildStep("Syntax check", _step_syntax_check),
        BuildStep("Unit tests", _step_unit_tests),
        BuildStep("Build GUI EXE", _step_build_exe),
        BuildStep("Prepare package", _step_prepare_package),
    ]


def run_steps(
    steps: Sequence[BuildStep], ctx: BuildContext
) -> Optional[str]:
    """依次执行；返回失败步骤名，全部成功返回 None（失败后不再继续）。"""
    for index, step in enumerate(steps, start=1):
        print(f"[{index}/{len(steps)}] {step.name}")
        try:
            step.action(ctx)
        except Exception as error:  # noqa: BLE001 - 统一在此汇报是哪一步
            print(f"构建失败：步骤「{step.name}」出错：{error}", file=sys.stderr)
            return step.name
    return None


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    system_name: Optional[str] = None,
    runner: Optional[Runner] = None,
    root: Optional[Path] = None,
) -> int:
    try:
        require_windows(system_name)
    except BuildError as error:
        print(str(error), file=sys.stderr)
        return 1
    if tuple(sys.version_info[:2]) != SUPPORTED_PYTHON:
        print(
            "警告：工作流使用 Python 3.12，当前是 "
            f"{sys.version_info.major}.{sys.version_info.minor}，"
            "构建结果可能与云端不一致。",
            file=sys.stderr,
        )
    ctx = BuildContext(
        root=Path(root) if root is not None else Path(__file__).resolve().parent,
        python_exe=sys.executable,
        runner=runner if runner is not None else subprocess_runner,
    )
    failed = run_steps(build_steps(), ctx)
    if failed is not None:
        return 1
    dist = ctx.root / DIST_DIR
    print("构建完成：")
    print(f"  {dist / EXE_NAME}")
    print(f"  {dist / ZIP_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
