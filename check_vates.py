#!/usr/bin/env python3
from __future__ import annotations

import gc
import importlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RELEASE = ROOT / "target" / "release"


def load_vates_core_via_windows_dll() -> bool:
    dll = RELEASE / "vates_core.dll"
    if not dll.is_file():
        return False
    pyd = RELEASE / "vates_core.pyd"
    ok = False
    mod_ref = None
    try:
        shutil.copy2(dll, pyd)
        if str(RELEASE) not in sys.path:
            sys.path.insert(0, str(RELEASE))
        mod_ref = importlib.import_module("vates_core")
        ok = hasattr(mod_ref, "encode_tensor") and hasattr(mod_ref, "decode_tensor")
    except Exception:
        ok = False
    finally:
        if mod_ref is not None:
            del mod_ref
        sys.modules.pop("vates_core", None)
        gc.collect()
        try:
            pyd.unlink(missing_ok=True)
        except OSError:
            pass
    return ok


def load_vates_core_via_sys_path_only() -> bool:
    try:
        if str(RELEASE) not in sys.path:
            sys.path.insert(0, str(RELEASE))
        mod = importlib.import_module("vates_core")
        return hasattr(mod, "encode_tensor")
    except Exception:
        return False


def probe_nodes_module() -> bool:
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
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
        core_ok = load_vates_core_via_sys_path_only()

    if not core_ok:
        print("FAIL")
        return 1

    if not probe_nodes_module():
        print("FAIL")
        return 1

    print("Vates Core Bridge: SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
