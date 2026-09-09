"""插件仓当前 V3 运行环境回归入口（本仓只维护 V3 插件）。

本仓没有 v1/v2 目录，直接按 tests/ci、tests/v3 分组在独立子进程运行，避免同名
插件包互相污染。测试引导逻辑委托 MoviePilot 主程序 app/testing.bootstrap。
"""
import subprocess
import sys
from pathlib import Path

# 本文件位于 tests/ 下：其父为 tests 目录，再上一级为插件仓根
_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent


def _contains_tests(path: Path) -> bool:
    """判断目录中是否存在 pytest 用例文件。"""
    return path.is_dir() and any(path.rglob("test_*.py"))


def _generation_targets(generation: str) -> list[Path]:
    """返回一个独立 pytest 会话需要执行的测试目标。"""
    target = _TESTS_DIR / generation
    return [target] if _contains_tests(target) else []


def _run_generation(generation: str, extra_args: list) -> int:
    """在独立子进程运行一个测试分组；该组无用例则跳过。"""
    targets = _generation_targets(generation)
    if not targets:
        return 0
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            *(str(target) for target in targets),
            *extra_args,
        ],
        cwd=str(_REPO_ROOT),
    )


if __name__ == "__main__":
    extra = sys.argv[1:]
    exit_code = 0
    for generation in ("ci", "v3"):
        rc = _run_generation(generation, extra)
        exit_code = exit_code or rc
    sys.exit(exit_code)