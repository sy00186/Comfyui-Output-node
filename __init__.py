"""
ComfyUI-Vates：包入口（将本仓库置于 ComfyUI `custom_nodes/` 时使用）。

依赖：已通过 `pip` / `install.py` / `maturin` 安装的 `vates_core` Python 扩展。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .vates_nodes import VatesLoadNode, VatesSaveNode, _ensure_vates_loaded
from .vates_server_hooks import register_vates_server_routes

try:
    _ensure_vates_loaded()
except RuntimeError as exc:
    print(
        "\n[Vates] ————————————————————————————————————————\n"
        "[Vates] 无法加载原生扩展 vates_core。\n"
        "[Vates] 在 **本仓库根目录**（含 Cargo.toml、install.py、vates_nodes.py）打开终端执行：\n"
        "[Vates]   python install.py\n"
        "[Vates] 然后 **重启 ComfyUI**。\n"
        "[Vates] ————————————————————————————————————————\n",
        file=sys.stderr,
    )
    raise exc


def _print_vates_loaded_banner() -> None:
    from . import vates_nodes as vn

    vc = vn.vates_core
    ver = getattr(vc, "__version__", "?")
    print(
        f"[Vates] Vates 核心 (vates_core) 加载成功，版本 {ver}（节点期望 {vn.VATES_EXPECTED_CORE_VERSION}）。",
        flush=True,
    )


_print_vates_loaded_banner()

_PKG = Path(__file__).resolve().parent
_WEB = _PKG / "web"
WEB_DIRECTORY = str(_WEB) if (_WEB / "vates_dct_drop.js").is_file() else ""

NODE_CLASS_MAPPINGS = {
    "VatesSaveNode": VatesSaveNode,
    "VatesLoadNode": VatesLoadNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "VatesSaveNode": "Vates · Save (.dct)",
    "VatesLoadNode": "Vates · Load (.dct)",
}

register_vates_server_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
