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
            root = root.resolve()
            key = str(root)
            if key not in sys.path:
                sys.path.insert(0, key)
            from vates_import_shim import register_vates_package

            register_vates_package(root)
            return
    raise RuntimeError(
        "ComfyUI-Vates：未找到 vates_nodes.py。请将本目录置于 "
        "dct-core/custom_nodes/ComfyUI-Vates/，或与 dct-core 仓库同级（如 comfyui-work/ComfyUI/custom_nodes/ 与 comfyui-work/dct-core/）。"
    )


_ensure_vates_nodes_importable()
import vates_nodes  # noqa: E402
from vates_nodes import (  # noqa: E402
    VatesLoadAndPreview,
    VatesLoadNode,
    VatesSaveNode,
    _ensure_vates_loaded,
)
from vates_server_hooks import register_vates_server_routes  # noqa: E402

try:
    _ensure_vates_loaded()
except RuntimeError as exc:
    print(
        "\n[Vates] ————————————————————————————————————————\n"
        "[Vates] 无法加载原生扩展 vates_core。\n"
        "[Vates] 在 **dct-core 仓库根目录**（含 Cargo.toml、install.py、vates_nodes.py）执行：\n"
        "[Vates]   python install.py\n"
        "[Vates] 环境变量 VATES_POST_COPY_DIR 可指向本节点目录，以便把 .so/.pyd 复制到与 vates_nodes.py 同级。\n"
        "[Vates] 然后 **重启 ComfyUI**。\n"
        f"[Vates] 详情: {exc}\n"
        "[Vates] ————————————————————————————————————————\n",
        file=sys.stderr,
    )
    raise exc

_vc = vates_nodes.vates_core
print(
    f"[Vates] Vates 核心 (vates_core) 加载成功，版本 {getattr(_vc, '__version__', '?')}"
    f"（节点期望 {vates_nodes.VATES_EXPECTED_CORE_VERSION}）。",
    flush=True,
)
del _vc


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
    "VatesLoadAndPreview": VatesLoadAndPreview,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "VatesSaveNode": "Vates · Save (.dct)",
    "VatesLoadNode": "Vates · Load (.dct)",
    "VatesLoadAndPreview": "Vates · Load & Preview",
}

WEB_DIRECTORY = _vates_web_dir()
register_vates_server_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
