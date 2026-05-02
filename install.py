#!/usr/bin/env python3
"""
Vates 一键安装：编译原生扩展并做「物理对齐」，使命令行当前 Python 可 `import vates_core`。

流程（按顺序）：
1. 已能 import 则直接成功退出；
2. 尝试 `custom_nodes/ComfyUI-Vates/wheels/` 下本地 wheel + pip；
3. **cargo build --release --no-default-features --features python**（显式打开 PyO3；**不要**单独使用 `--no-default-features` 而不加 `--features python`，否则不会生成可导入模块）；
4. 将 `target/release/` 下产物复制/对齐到仓库根目录（与 `vates_nodes.py` 同级），便于 ComfyUI 加载；
5. 可选：`maturin develop --release` 兜底。

说明：
- 本脚本默认不访问网络（除非 pip/maturin 自身配置源）。
- ComfyUI 用户：在 **本仓库根目录**（含 `vates_nodes.py` 与 `Cargo.toml`）执行一次 `python install.py`，然后重启 ComfyUI。
- 若实际加载的 `vates_nodes.py` 在**别的目录**（例如 `Comfyui-Output-node/`），可设置环境变量 **`VATES_POST_COPY_DIR`** 指向该目录，脚本会把编好的 `.so` / `.pyd` **额外复制**过去，与那份 `vates_nodes.py` 同级；多个目录可用系统路径分隔符连接（Linux `:`，Windows `;`）。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WHEELS_DIR = ROOT / "custom_nodes" / "ComfyUI-Vates" / "wheels"
RELEASE = ROOT / "target" / "release"

from vates_repo_meta import expected_vates_core_version


def _copy_native_artifact(src: Path, dst: Path) -> None:
    """
    复制编译产物到目标路径。Windows 上若目标 .pyd 正被 Python/ComfyUI 占用，
    直接 shutil.copy2 可能报 PermissionError，故先写入临时文件再 os.replace。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32" and dst.is_file():
        fd, tmp_name = tempfile.mkstemp(suffix=dst.suffix, dir=str(dst.parent))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return
    shutil.copy2(src, dst)


def _post_copy_dest_dirs() -> list[Path]:
    raw = (os.environ.get("VATES_POST_COPY_DIR") or "").strip()
    if not raw:
        return []
    out: list[Path] = []
    for part in raw.split(os.pathsep):
        p = part.strip()
        if not p:
            continue
        out.append(Path(p).expanduser().resolve())
    return out


def _mirror_aligned_files_to_post_copy(aligned_files: list[str]) -> None:
    """把已对齐到仓库根的二进制再复制到 VATES_POST_COPY_DIR，供其他路径下的 vates_nodes 加载。"""
    dirs = _post_copy_dest_dirs()
    if not dirs or not aligned_files:
        return
    for d in dirs:
        if not d.is_dir():
            print(f"[Vates install] 警告: VATES_POST_COPY_DIR 不是目录，已跳过: {d}", flush=True)
            continue
        for fp in aligned_files:
            src = Path(fp)
            if not src.is_file():
                continue
            dst = d / src.name
            _copy_native_artifact(src, dst)
            if sys.platform != "win32" and dst.suffix == ".so":
                try:
                    os.chmod(
                        dst,
                        stat.S_IRUSR
                        | stat.S_IWUSR
                        | stat.S_IXUSR
                        | stat.S_IRGRP
                        | stat.S_IXGRP
                        | stat.S_IROTH
                        | stat.S_IXOTH,
                    )
                except OSError:
                    pass
            print(f"[Vates install] 已复制到 ComfyUI/节点目录: {dst}", flush=True)


def _pythonpath_for_verify() -> str:
    parts = [str(ROOT)] + [str(p) for p in _post_copy_dest_dirs()]
    prev = os.environ.get("PYTHONPATH", "")
    if prev:
        parts.append(prev)
    return os.pathsep.join(parts)


def _pick_windows_native_artifact(release_dir: Path) -> Path | None:
    if not release_dir.is_dir():
        return None
    tagged = list(release_dir.glob("vates_core.cp*.pyd"))
    if tagged:
        return max(tagged, key=lambda p: p.stat().st_mtime)
    dll = release_dir / "vates_core.dll"
    if dll.is_file():
        return dll
    plain = release_dir / "vates_core.pyd"
    if plain.is_file():
        return plain
    return None


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _vates_core_version_usable() -> bool:
    """已加载的 vates_core 须带 __version__ 且与 pyproject 一致。"""
    if not _has_module("vates_core"):
        return False
    try:
        import vates_core as vc
    except ImportError:
        return False
    return getattr(vc, "__version__", None) == expected_vates_core_version()


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
    cmd = [sys.executable, "-m", "pip", "install", "--force-reinstall", str(wheel)]
    print(f"[Vates install] 执行: {' '.join(cmd)}", flush=True)
    try:
        subprocess.check_call(cmd, cwd=str(ROOT))
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Vates install] pip 安装失败: {e}", flush=True)
        return False


def _cargo_build_release() -> bool:
    if shutil.which("cargo") is None or shutil.which("rustc") is None:
        print(
            "[Vates install] 未找到 rustc 或 cargo，无法从源码编译 vates_core。\n"
            "  请安装 Rust 工具链：https://rustup.rs/\n"
            "  • Linux / macOS：curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh\n"
            "  • Windows：https://www.rust-lang.org/tools/install\n"
            "  安装后**新开终端**，在本仓库根目录执行: python install.py",
            flush=True,
        )
        return False
    for cmd in (["rustc", "--version"], ["cargo", "--version"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=str(ROOT))
            if r.returncode == 0 and r.stdout.strip():
                print(f"[Vates install] {r.stdout.strip()}", flush=True)
        except (subprocess.TimeoutExpired, OSError):
            pass
    # 与「仅禁用默认再显式启用 python」等价于默认构建；单独 --no-default-features 不会生成 PyO3 模块。
    cmd = [
        "cargo",
        "build",
        "--release",
        "--no-default-features",
        "--features",
        "python",
    ]
    print(f"[Vates install] 执行: {' '.join(cmd)} （目录 {ROOT}）", flush=True)
    try:
        subprocess.check_call(cmd, cwd=str(ROOT))
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Vates install] cargo build 失败: {e}", flush=True)
        return False


def _align_native_artifacts_to_root() -> list[str]:
    """
    将编译产物复制到仓库根目录，供 Python 以 `vates_core` 名称加载。
    返回已写入或已存在的、用于提示的路径列表。
    """
    done: list[str] = []
    if not RELEASE.is_dir():
        return done

    if sys.platform == "win32":
        src = _pick_windows_native_artifact(RELEASE)
        if src is not None:
            dst = ROOT / "vates_core.pyd"
            _copy_native_artifact(src, dst)
            done.append(str(dst))
    elif sys.platform == "darwin":
        dylib = RELEASE / "libvates_core.dylib"
        dst = ROOT / "vates_core.so"
        if dylib.is_file():
            _copy_native_artifact(dylib, dst)
            os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            done.append(str(dst))
        else:
            lib_so = RELEASE / "libvates_core.so"
            if lib_so.is_file():
                _copy_native_artifact(lib_so, dst)
                os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
                done.append(str(dst))
            else:
                alt = RELEASE / "vates_core.so"
                if alt.is_file():
                    _copy_native_artifact(alt, dst)
                    os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
                    done.append(str(dst))
    else:
        lib_so = RELEASE / "libvates_core.so"
        if lib_so.is_file():
            dst = ROOT / "vates_core.so"
            _copy_native_artifact(lib_so, dst)
            os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            done.append(str(dst))
        else:
            # 个别环境已直接命名
            alt = RELEASE / "vates_core.so"
            if alt.is_file():
                dst = ROOT / "vates_core.so"
                _copy_native_artifact(alt, dst)
                os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
                done.append(str(dst))

    return done


def _try_maturin_develop() -> bool:
    if shutil.which("maturin") is None or shutil.which("cargo") is None:
        print("[Vates install] 未找到 maturin 或 cargo，跳过 maturin。", flush=True)
        return False
    cmd = ["maturin", "develop", "--release"]
    print(f"[Vates install] 执行: {' '.join(cmd)} （目录 {ROOT}）", flush=True)
    try:
        subprocess.check_call(cmd, cwd=str(ROOT))
        return True
    except subprocess.CalledProcessError as e:
        print(f"[Vates install] maturin develop 失败: {e}", flush=True)
        return False


def _verify_import_subprocess() -> bool:
    exp = expected_vates_core_version()
    script = (
        "import vates_core as vc; "
        f"exp={exp!r}; "
        "got=getattr(vc, '__version__', None); "
        "assert got == exp, (got, exp); "
        "assert hasattr(vc, 'encode_tensor')"
    )
    try:
        subprocess.check_call(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": _pythonpath_for_verify()},
        )
        return True
    except subprocess.CalledProcessError:
        return False


def main() -> int:
    print(
        f"[Vates install] Python {sys.version.split()[0]}, platform={sys.platform}, cwd={os.getcwd()}",
        flush=True,
    )

    if _vates_core_version_usable():
        print("[Vates install] vates_core 已可用且版本匹配，无需安装。", flush=True)
        print("vates_core 已就绪；请重启 ComfyUI。", flush=True)
        return 0

    if _has_module("vates_core"):
        print(
            "[Vates install] 已存在 vates_core 模块但 **版本不匹配** 或缺少 __version__；将继续尝试 wheel / 编译重装…",
            flush=True,
        )
    wheel = _pick_wheel()
    if wheel is not None:
        print(f"[Vates install] 尝试从本地 wheel 安装: {wheel.name}", flush=True)
        if _run_pip_install_wheel(wheel) and _vates_core_version_usable():
            print("[Vates install] 通过 wheel 安装成功。", flush=True)
            print("vates_core 已就绪，请手动重启 ComfyUI。", flush=True)
            return 0

    if _cargo_build_release():
        copied = _align_native_artifacts_to_root()
        if copied:
            print("[Vates install] 已对齐到仓库根目录:", flush=True)
            for p in copied:
                print(f"         {p}", flush=True)
            _mirror_aligned_files_to_post_copy(copied)
        else:
            print(
                f"[Vates install] 警告: 在 {RELEASE} 未找到 libvates_core.so / vates_core.dll / vates_core*.pyd",
                flush=True,
            )
        if _verify_import_subprocess():
            print("[Vates install] cargo 构建 + 对齐后 `import vates_core` 验证通过。", flush=True)
            print("vates_core 已就绪，请重启 ComfyUI。", flush=True)
            return 0
        print("[Vates install] 对齐后仍无法 import，将尝试 maturin…", flush=True)

    if _try_maturin_develop() and _vates_core_version_usable():
        print("[Vates install] 通过 maturin develop 构建成功。", flush=True)
        print("vates_core 已就绪，请手动重启 ComfyUI。", flush=True)
        return 0

    print(
        "[Vates install] 自动安装失败。请检查：\n"
        "  • 已安装 Rust (cargo) 与当前 Python 对应的构建依赖；\n"
        f"  • 在仓库根目录执行: python \"{ROOT / 'install.py'}\"\n"
        "  • 或放置匹配的 vates_core-*.whl 到 wheels/ 后重试；\n"
        "  • 或手动: maturin develop --release\n"
        "完成后重启 ComfyUI。",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
