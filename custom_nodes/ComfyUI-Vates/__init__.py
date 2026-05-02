"""
ComfyUI-Vates 插件入口。

节点实现单源：`dct-core/vates_nodes.py`（经下方路径解析后 `import vates_nodes`）。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_vates_nodes_importable() -> None:
    here = Path(__file__).resolve().parent
    roots = (
        here.parent.parent.parent,
        here.parent.parent.parent.parent / "dct-core",
    )
    for root in roots:
        if (root / "vates_nodes.py").is_file():
            key = str(root)
            if key not in sys.path:
                sys.path.insert(0, key)
            return
    raise RuntimeError(
        "ComfyUI-Vates：未找到 vates_nodes.py。请将本目录置于 "
        "dct-core/custom_nodes/ComfyUI-Vates/，或与 dct-core 仓库同级（如 comfyui-work/ComfyUI/custom_nodes/ 与 comfyui-work/dct-core/）。"
    )


_ensure_vates_nodes_importable()
from vates_nodes import VatesLoadNode, VatesSaveNode  # noqa: E402
from vates_server_hooks import register_vates_server_routes  # noqa: E402

def _vates_web_dir() -> str:
    here = Path(__file__).resolve().parent
    for root in (here.parent.parent.parent, here.parent.parent.parent.parent / "dct-core"):
        w = root / "web"
        if (w / "vates_dct_drop.js").is_file():
            return str(w)
    return ""

NODE_CLASS_MAPPINGS = {
    "VatesSaveNode": VatesSaveNode,
    "VatesLoadNode": VatesLoadNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "VatesSaveNode": "Vates · Save (.dct)",
    "VatesLoadNode": "Vates · Load (.dct)",
}

WEB_DIRECTORY = _vates_web_dir()
register_vates_server_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
