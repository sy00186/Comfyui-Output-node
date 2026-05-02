"""
ComfyUI-Vates 插件入口。

依赖：`import vates_core`（本仓库：Maturin / `install.py` / wheels）。
"""

from __future__ import annotations

from .nodes import VatesLoadNode, VatesSaveNode

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
