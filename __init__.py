"""
ComfyUI-Vates：包入口（将本仓库置于 ComfyUI `custom_nodes/` 时使用）。

依赖：已通过 `pip` / `install.py` / `maturin` 安装的 `vates_core` Python 扩展。
"""

from __future__ import annotations

from .vates_nodes import VatesLoadNode, VatesSaveNode

NODE_CLASS_MAPPINGS = {
    "VatesSaveNode": VatesSaveNode,
    "VatesLoadNode": VatesLoadNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "VatesSaveNode": "Vates · Save (.dct)",
    "VatesLoadNode": "Vates · Load (.dct)",
}

WEB_DIRECTORY = ""

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
