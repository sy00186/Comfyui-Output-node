#!/usr/bin/env python3
from __future__ import annotations

import gc
import importlib
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASE = ROOT / "target" / "release"

from vates_repo_meta import expected_vates_core_version


def _extension_version_matches(mod: object) -> bool:
    return getattr(mod, "__version__", None) == expected_vates_core_version()


def _pick_windows_native_artifact(release_dir: Path) -> Path | None:
    """
    优先：vates_core.cp*.pyd（多来自 maturin）；
    其次：cargo cdylib 产出的 vates_core.dll；
    最后：裸 vates_core.pyd。
    避免 target/release 里陈旧的 .pyd 排在 glob 前面、盖过新编译的 .dll。
    """
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


def _chmod_exec_so(path: Path) -> None:
    try:
        os.chmod(
            path,
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


def _align_posix_release_to_root() -> bool:
    """Linux / macOS：将 target/release 下产物对齐为根目录 vates_core.so。"""
    dst = ROOT / "vates_core.so"
    if sys.platform == "darwin":
        dylib = RELEASE / "libvates_core.dylib"
        if dylib.is_file():
            shutil.copy2(dylib, dst)
            _chmod_exec_so(dst)
            return True
    lib = RELEASE / "libvates_core.so"
    alt = RELEASE / "vates_core.so"
    if lib.is_file():
        shutil.copy2(lib, dst)
        _chmod_exec_so(dst)
        return True
    if alt.is_file():
        shutil.copy2(alt, dst)
        _chmod_exec_so(dst)
        return True
    return False


def load_vates_core_via_windows_dll() -> bool:
    src = _pick_windows_native_artifact(RELEASE)
    if src is None:
        return False
    dst_root_pyd = ROOT / "vates_core.pyd"
    shutil.copy2(src, dst_root_pyd)
    ok = False
    mod_ref = None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        mod_ref = importlib.import_module("vates_core")
        ok = (
            _extension_version_matches(mod_ref)
            and hasattr(mod_ref, "encode_tensor")
            and hasattr(mod_ref, "encode_batch")
            and hasattr(mod_ref, "encode_batch_async")
            and hasattr(mod_ref, "get_pending_tasks")
            and hasattr(mod_ref, "await_pending_writes")
            and hasattr(mod_ref, "decode_tensor")
            and hasattr(mod_ref, "decode_tensor_with_workflow")
            and hasattr(mod_ref, "read_embedded_workflow_json")
        )
    except Exception:
        ok = False
    finally:
        if mod_ref is not None:
            del mod_ref
        sys.modules.pop("vates_core", None)
        gc.collect()
    return ok


def load_vates_core_posix() -> bool:
    _align_posix_release_to_root()
    if not (ROOT / "vates_core.so").is_file():
        return False
    mod = None
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        mod = importlib.import_module("vates_core")
        return (
            _extension_version_matches(mod)
            and hasattr(mod, "encode_tensor")
            and hasattr(mod, "encode_batch")
            and hasattr(mod, "decode_tensor_with_workflow")
        )
    except Exception:
        return False
    finally:
        if mod is not None:
            del mod
        sys.modules.pop("vates_core", None)
        gc.collect()


def probe_nodes_module() -> bool:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from vates_import_shim import register_vates_package

        register_vates_package(ROOT)
        import vates_nodes  # noqa: F401
    except ModuleNotFoundError as e:
        return e.name == "folder_paths"
    except Exception:
        return False
    return True


def main() -> int:
    if sys.platform == "win32":
        core_ok = load_vates_core_via_windows_dll()
    else:
        core_ok = load_vates_core_posix()

    if not core_ok:
        print("FAIL")
        print(f"  cwd={os.getcwd()}", file=sys.stderr)
        exp = expected_vates_core_version()
        print(f"  期望 vates_core.__version__ == {exp!r}（与 pyproject 一致）。若不匹配请重新 python install.py。", file=sys.stderr)
        print(f"  expected: {ROOT / 'vates_core.so'} or {RELEASE / 'libvates_core.so'} (Linux)", file=sys.stderr)
        print(f"            or {RELEASE / 'libvates_core.dylib'} (macOS)", file=sys.stderr)
        print(f"            {ROOT / 'vates_core.pyd'} or {RELEASE / 'vates_core*.pyd'} (Windows)", file=sys.stderr)
        return 1

    if not probe_nodes_module():
        print("FAIL")
        return 1

    print("Vates Core Bridge: SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
