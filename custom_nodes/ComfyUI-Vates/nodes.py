"""
Vates：ComfyUI 节点（保存 / 加载 .dct）。

约定：
- ComfyUI IMAGE 为 torch.Tensor，形状 [Batch, Height, Width, Channels]（BHWC）。
- Rust 扩展 vates_core.encode_tensor 需要 float32、C 连续、形状 [C, H, W]（CHW）的 NumPy 数组。
"""

from __future__ import annotations

import os
import time

import folder_paths
import numpy as np
import torch

try:
    import vates_core
except ImportError as exc:  # pragma: no cover
    vates_core = None  # type: ignore[assignment]
    _VATES_IMPORT_ERROR = exc
else:
    _VATES_IMPORT_ERROR = None


def _ensure_vates_loaded() -> None:
    """确认原生扩展 vates_core 已载入。"""
    if vates_core is None:
        raise RuntimeError(
            "未能导入 vates_core（Rust 扩展）。请在仓库根目录执行 `python install.py`，"
            "或 `pip install` 对应平台的 .whl，或使用 `maturin develop --release` 从源码构建后重启 ComfyUI。"
        ) from _VATES_IMPORT_ERROR


class VatesSaveNode:
    """将 IMAGE 批量写入 Vates `.dct`（每帧单独文件）。"""

    def __init__(self) -> None:
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802 — ComfyUI 惯例
        return {
            "required": {
                "images": ("IMAGE",),
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "vates_tensor",
                        "tooltip": "文件名前缀；实际为 前缀_时间戳_帧序号.dct",
                    },
                ),
            }
        }

    RETURN_TYPES: tuple = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Vates/IO"

    def save(self, images: torch.Tensor, filename_prefix: str) -> dict:
        _ensure_vates_loaded()

        if images.ndim != 4:
            raise ValueError(f"IMAGE 张量应为 4 维 [B,H,W,C]，当前 ndim={images.ndim}")

        batch = int(images.shape[0])
        if batch <= 0:
            raise ValueError("Batch 维度为 0，无可保存的帧")

        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        safe_prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in filename_prefix.strip()) or "vates_tensor"

        saved_paths: list[str] = []

        for i in range(batch):
            frame = images[i]
            frame_chw = frame.permute(2, 0, 1).contiguous()
            frame_cpu = frame_chw.detach().cpu()
            if frame_cpu.dtype != torch.float32:
                frame_cpu = frame_cpu.float()

            frame_np = frame_cpu.numpy()
            if not frame_np.flags["C_CONTIGUOUS"]:
                frame_np = np.ascontiguousarray(frame_np, dtype=np.float32)

            fname = f"{safe_prefix}_{ts}_{i:05d}.dct"
            filepath = os.path.join(self.output_dir, fname)

            vates_core.encode_tensor(frame_np, filepath)
            saved_paths.append(filepath)
            print(f"[Vates] 成功保存 DCT 文件 ({i + 1}/{batch}): {filepath}", flush=True)

        preview = saved_paths[0] if len(saved_paths) == 1 else f"{len(saved_paths)} 个文件（首帧）：{saved_paths[0]}"
        return {"ui": {"text": (preview,)}}


class VatesLoadNode:
    """从 `.dct` 恢复为 ComfyUI IMAGE（单张，Batch=1）。"""

    def __init__(self) -> None:
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {
                "dct_path": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": ".dct 完整路径，或相对于 ComfyUI output 目录的相对路径",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "load"
    OUTPUT_NODE = False
    CATEGORY = "Vates/IO"

    def load(self, dct_path: str) -> tuple[torch.Tensor]:
        _ensure_vates_loaded()

        raw = (dct_path or "").strip()
        if not raw:
            raise ValueError("dct_path 不能为空")

        filepath = raw if os.path.isabs(raw) else os.path.join(self.output_dir, raw)
        filepath = os.path.normpath(filepath)

        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"未找到 DCT 文件: {filepath}")

        arr_chw = vates_core.decode_tensor(filepath)
        if not isinstance(arr_chw, np.ndarray):
            arr_chw = np.asarray(arr_chw, dtype=np.float32)
        if arr_chw.dtype != np.float32:
            arr_chw = arr_chw.astype(np.float32, copy=False)

        if arr_chw.ndim != 3:
            raise ValueError(f"解码数组应为 CHW（ndim=3），当前 ndim={arr_chw.ndim}")

        tensor_chw = torch.from_numpy(np.ascontiguousarray(arr_chw))
        tensor_hwc = tensor_chw.permute(1, 2, 0).contiguous()
        image_bhwc = tensor_hwc.unsqueeze(0)

        print(f"[Vates] 成功加载 DCT 文件: {filepath}，形状 {tuple(image_bhwc.shape)}", flush=True)
        return (image_bhwc,)


__all__ = ["VatesLoadNode", "VatesSaveNode"]
