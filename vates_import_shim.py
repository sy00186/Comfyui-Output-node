"""ComfyUI 经 `sys.path` 加载仓库根目录时，将 `vates_nodes` 挂到合成包下以支持相对导入。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PKG = "__comfyui_vates__"


def _purge_vates_shim_modules() -> None:
    """卸载合成包及别名，避免热重载 / 切换 root 时新旧模块混用。"""
    drop = [
        m
        for m in list(sys.modules.keys())
        if m == _PKG or m.startswith(f"{_PKG}.") or m == "vates_nodes"
    ]
    for m in drop:
        sys.modules.pop(m, None)


def register_vates_package(root: Path) -> None:
    """注册合成包并将 `vates_nodes` 挂到 `__comfyui_vates__` 下。

    调用方须先将 ``str(root.resolve())`` 置于 ``sys.path``（通常插入到首位），
    以便本函数内可 ``import vates_repo_meta`` 等根目录模块。
    """
    root = root.resolve()
    key = str(root)

    existing = sys.modules.get(_PKG)
    prev_root = getattr(existing, "_vates_root", None) if existing else None
    if prev_root is not None and prev_root != key:
        _purge_vates_shim_modules()

    existing = sys.modules.get(_PKG)
    if existing is not None and getattr(existing, "_vates_root", None) == key:
        vn = sys.modules.get(f"{_PKG}.vates_nodes")
        if vn is not None:
            sys.modules["vates_nodes"] = vn
        return

    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [key]
    pkg._vates_root = key  # type: ignore[attr-defined]
    sys.modules[_PKG] = pkg

    def _exec(name: str) -> object:
        fq = f"{_PKG}.{name}"
        path = root / f"{name}.py"
        spec = importlib.util.spec_from_file_location(fq, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = _PKG
        sys.modules[fq] = mod
        spec.loader.exec_module(mod)
        return mod

    _exec("vates_repo_meta")
    _exec("vates_nodes")
    sys.modules["vates_nodes"] = sys.modules[f"{_PKG}.vates_nodes"]
