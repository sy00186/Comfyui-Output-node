#!/usr/bin/env python3
"""
Vates 安装入口：确保当前 Python 环境可 `import vates_core`。

策略（按顺序尝试）：
1. 已安装则直接成功退出；
2. 使用 custom_nodes/ComfyUI-Vates/wheels/ 下与当前解释器版本最匹配的 *.whl；
3. 若存在 rustc + maturin，则在仓库根目录执行 `maturin develop --release`；
4. 否则打印人工指引。

说明：
- 本脚本**不发起任何网络请求**（除非 pip/maturin 自身配置镜像源）；
- 适合 RunningHub 工单场景：客服放置 wheel 后执行一次 `python install.py`。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WHEELS_DIR = ROOT / "custom_nodes" / "ComfyUI-Vates" / "wheels"


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _py_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def _pick_wheel() -> Path | None:
    if not WHEELS_DIR.is_dir():
        return None
    wheels = sorted(WHEELS_DIR.glob("vates_core*.whl"))
    if not wheels:
        wheels = sorted(WHEELS_DIR.glob("*.whl"))
    if not wheels:
        return None
    tag = _py_tag()
    tagged = [w for w in wheels if tag in w.name]
    return tagged[0] if tagged else wheels[0]


def _run_pip_install_wheel(wheel: Path) -> bool:
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--force-reinstall",
        str(wheel),
    ]
    print(f"[Vates install] 执行: {' '.join(cmd)}", flush=True)
    try:
        subprocess.check_call(cmd, cwd=str(ROOT))
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Vates install] pip 安装失败: {e}", flush=True)
        return False


def _try_maturin_develop() -> bool:
    if shutil.which("maturin") is None or shutil.which("cargo") is None:
        print("[Vates install] 未找到 maturin 或 cargo，跳过源码编译。", flush=True)
        return False
    cmd = ["maturin", "develop", "--release"]
    print(f"[Vates install] 执行: {' '.join(cmd)} （目录 {ROOT}）", flush=True)
    try:
        subprocess.check_call(cmd, cwd=str(ROOT))
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Vates install] maturin develop 失败: {e}", flush=True)
        return False


def main() -> int:
    print(f"[Vates install] Python {sys.version.split()[0]}, platform={sys.platform}", flush=True)

    if _has_module("vates_core"):
        print("[Vates install] vates_core 已可用，无需安装。", flush=True)
        print("vates_core 已就绪；请重启 ComfyUI。", flush=True)
        return 0

    wheel = _pick_wheel()
    if wheel is not None:
        print(f"[Vates install] 尝试从本地 wheel 安装: {wheel.name}", flush=True)
        if _run_pip_install_wheel(wheel) and _has_module("vates_core"):
            print("[Vates install] 通过 wheel 安装成功。", flush=True)
            print("vates_core 已就绪，请手动重启 ComfyUI。", flush=True)
            return 0

    if _try_maturin_develop() and _has_module("vates_core"):
        print("[Vates install] 通过 maturin develop 构建成功。", flush=True)
        print("vates_core 已就绪，请手动重启 ComfyUI。", flush=True)
        return 0

    print(
        "[Vates install] 自动安装失败。请手动：\n"
        "  1) 将匹配当前 Python 的 vates_core-*.whl 放入 wheels/ 目录后重试；或\n"
        "  2) 在仓库根目录执行: maturin develop --release；或\n"
        "  3) pip install dist/*.whl（自行构建产物）。\n"
        "完成后请重启 ComfyUI。",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
