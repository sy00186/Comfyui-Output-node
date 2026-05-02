"""
Vates：ComfyUI 节点（保存 / 加载 .dct）。

- 调试计时（纳秒）：设置环境变量 **`VATES_ENCODE_TIMING=1`**（Rust 侧 `encode_tensor` 与 Python `_encode_dct_frame` 均会输出墙钟）。
- ComfyUI IMAGE 为 torch.Tensor，形状 [Batch, Height, Width, Channels]（BHWC）。
- Rust 扩展 vates_core.encode_tensor：CHW float32 C 连续；并写入 32B VATS 头字段 mode、fps、B/C/H/W（详见 Rust 文档）。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import secrets
import sys
import threading
import time
from datetime import datetime

import folder_paths
import numpy as np
import torch

vates_core = None  # type: ignore[assignment, misc]
logger = logging.getLogger(__name__)

# `DctHeader.reserved`：低 7 位为容器类型；最高位 0x80 表示头后存在 META+zstd(工作流 JSON)（P5）
VATES_RESERVED_WORKFLOW_JSON_FLAG: int = 0x80
# P2：`reserved` 低 7 位 — 1 = 旧版多块无块校验；2 = P2 + 每块 XXH3（可 append）
VATES_CONTAINER_P2_LEGACY: int = 1
VATES_CONTAINER_P2_XXH3: int = 2

# 与 Rust 头字段 `mode` 一致：0 Image / 1 Video / 2 Stream
SAVE_MODE_TO_ID = {
    "Image (Sequence)": 0,
    "Video (Batch)": 1,
    "Streaming (Append)": 2,
}


def _ensure_vates_loaded() -> None:
    """确认原生扩展 vates_core 已载入（惰性导入，并将插件目录置于 sys.path 首位）。"""
    global vates_core
    if vates_core is not None:
        return
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    try:
        import vates_core as vc
        vates_core = vc
    except ImportError as e:
        raise RuntimeError(
            f"Vates Core 导入失败。目录 {current_dir} 内容: {os.listdir(current_dir)}。详情: {e}"
        ) from e
    _register_atexit_flush_once()


_ATEXIT_FLUSH_REGISTERED = False


def _register_atexit_flush_once() -> None:
    global _ATEXIT_FLUSH_REGISTERED
    if _ATEXIT_FLUSH_REGISTERED:
        return
    _ATEXIT_FLUSH_REGISTERED = True
    atexit.register(_flush_vates_pending_at_exit)


def _flush_vates_pending_at_exit() -> None:
    try:
        if vates_core is None:
            return
        vates_core.await_pending_writes()
        logger.info("Vates: 进程退出前已全部 flush 后台写入（await_pending_writes）。")
    except Exception as exc:
        logger.warning("Vates: 退出时 await_pending_writes 异常: %s", exc)


def _make_run_unique() -> str:
    """去竞态：微秒时间戳 + 随机短码，单次 save 调用内复用同一返回值。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{ts}_{secrets.token_hex(4)}"


def _sanitize_segment(raw: str, *, fallback: str) -> str:
    s = (raw or "").strip()
    out = "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
    return out if out else fallback


def _tensor_bhwc_to_chw_numpy(images: torch.Tensor, index: int) -> np.ndarray:
    """自 BHWC 取一帧 → CHW float32；尽量单次 contiguous，减少中间变量。"""
    x = images[index].permute(2, 0, 1).detach()
    if x.device.type != "cpu":
        x = x.cpu()
    if x.dtype != torch.float32:
        x = x.float()
    x = x.contiguous()
    return np.ascontiguousarray(x.numpy(), dtype=np.float32)


def _embed_workflow_json(prompt: object, extra_pnginfo: object) -> str | None:
    """打包 ComfyUI `prompt` / `extra_pnginfo` 为 UTF-8 JSON 字符串，供 Rust 嵌入。"""
    try:
        blob: dict[str, object] = {}
        if prompt is not None:
            blob["prompt"] = prompt
        if extra_pnginfo is not None:
            blob["extra_pnginfo"] = extra_pnginfo
        if not blob:
            return None
        return json.dumps(blob, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Vates: 工作流 JSON 序列化失败，跳过嵌入: %s", exc)
        return None


def _require_bhwc_images(images: torch.Tensor) -> int:
    """强制校验 4D BHWC；返回 batch 大小。"""
    if not isinstance(images, torch.Tensor):
        raise TypeError(f"images 须为 torch.Tensor，当前为 {type(images).__name__}")
    if images.ndim != 4:
        raise ValueError(f"IMAGE 张量须为 4 维 BHWC，当前 ndim={images.ndim}，shape={tuple(images.shape)}")
    batch = int(images.shape[0])
    if batch <= 0:
        raise ValueError("Batch 维度为 0，无可保存的帧")
    return batch


def _encode_dct_frame(
    frame_np: np.ndarray,
    filepath: str,
    *,
    mode_id: int,
    fps: float,
    metadata: str | None = None,
) -> None:
    """调用原生编码：入参 C 连续 float32，供 Rust 只读借用（P1 零拷贝路径）。"""
    frame_np = np.ascontiguousarray(frame_np, dtype=np.float32)
    timing = os.environ.get("VATES_ENCODE_TIMING", "").lower() in ("1", "true", "yes")
    if timing:
        t0 = time.perf_counter_ns()
        vates_core.encode_tensor(
            frame_np, filepath, int(mode_id), float(fps), 1, metadata
        )
        dt = time.perf_counter_ns() - t0
        print(f"[Vates P1] encode 墙钟 {dt} ns → {filepath}", flush=True)
    else:
        vates_core.encode_tensor(
            frame_np, filepath, int(mode_id), float(fps), 1, metadata
        )


def _after_pending_drops(prev_pending: int, message: str) -> None:
    """后台等待全局 pending 计数回落至 `prev_pending` 后打日志（与其它并发 save 叠在一起时近似「本轮」完成）。"""

    def run() -> None:
        try:
            while vates_core is not None and vates_core.get_pending_tasks() > prev_pending:
                time.sleep(0.02)
            logger.info("%s", message)
        except Exception as exc:
            logger.warning("Vates: 后台完成通知异常: %s", exc)

    threading.Thread(target=run, daemon=True).start()


def _can_append_streaming(filepath: str, arr_bhwc: np.ndarray, fps: float) -> bool:
    """已与盘上 P2+XXH3 Stream 文件对齐（C/H/W、fps、mode、reserved=2）则可 append。"""
    try:
        _batch, ch, hh, ww, mode, reserved, file_fps = vates_core.peek_dct_header(filepath)
    except Exception:
        return False
    if reserved & 0x7F != VATES_CONTAINER_P2_XXH3 or int(mode) != SAVE_MODE_TO_ID["Streaming (Append)"]:
        return False
    _b, H, W, C = arr_bhwc.shape
    if int(ch) != int(C) or int(hh) != int(H) or int(ww) != int(W):
        return False
    if abs(float(file_fps) - float(fps)) > 0.02:
        return False
    return True


def _streaming_legacy_p2_would_overwrite(
    filepath: str, arr_bhwc: np.ndarray, fps: float
) -> bool:
    """旧版 P2（无 XXH3）流式同名文件：若直接 encode 会截断覆盖，需用户先处理。"""
    try:
        _batch, ch, hh, ww, mode, reserved, file_fps = vates_core.peek_dct_header(filepath)
    except Exception:
        return False
    if (reserved & 0x7F) != VATES_CONTAINER_P2_LEGACY or int(mode) != SAVE_MODE_TO_ID["Streaming (Append)"]:
        return False
    _b, H, W, C = arr_bhwc.shape
    if int(ch) != int(C) or int(hh) != int(H) or int(ww) != int(W):
        return False
    return abs(float(file_fps) - float(fps)) <= 0.02


class VatesSaveNode:
    """将 IMAGE 批量写入 Vates `.dct`（支持序列 / 视频批处理临时帧 / 流式递增序号）。"""

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
                        "tooltip": "文件名段；与 stream_id、去竞态片段等组合，见各 save_mode 说明",
                    },
                ),
                "save_mode": (
                    (
                        "Image (Sequence)",
                        "Video (Batch)",
                        "Streaming (Append)",
                    ),
                    {"default": "Image (Sequence)"},
                ),
                "fps": (
                    "FLOAT",
                    {
                        "default": 24.0,
                        "min": 0.1,
                        "max": 240.0,
                        "step": 0.1,
                        "round": 0.01,
                    },
                ),
                "stream_id": (
                    "STRING",
                    {
                        "default": "default",
                        "tooltip": "Streaming 模式：与 filename_prefix 组成固定单文件 vates_stream__{id}__{prefix}.dct，多次运行在同一文件上物理追加",
                    },
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES: tuple = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Vates/IO"

    def _internal_video_packer(
        self,
        images: torch.Tensor,
        *,
        unique: str,
        safe_prefix: str,
        fps: float,
        batch: int,
        workflow_json: str | None,
    ) -> list[str]:
        """P2：整段 BHWC 单次异步 `encode_batch_async` → 单个 .dct（字典 + 分块 zstd，不阻塞 UI）。"""
        fname = f"{safe_prefix}_vbatch_{unique}.dct"
        filepath = os.path.join(self.output_dir, fname)
        x = images.detach()
        if x.device.type != "cpu":
            x = x.cpu()
        if x.dtype != torch.float32:
            x = x.float()
        x = x.contiguous()
        arr = np.ascontiguousarray(x.numpy(), dtype=np.float32)
        if arr.ndim != 4:
            raise ValueError(f"Video Batch 需要 BHWC ndim=4，当前 ndim={arr.ndim}")
        tag = f"{safe_prefix}_vbatch_{unique}"
        prev = vates_core.get_pending_tasks()
        print(f"Vates: [{tag}] saving in background...", flush=True)
        vates_core.encode_batch_async(
            arr,
            filepath,
            float(fps),
            False,
            SAVE_MODE_TO_ID["Video (Batch)"],
            workflow_json,
        )
        _after_pending_drops(
            prev,
            f"Vates: [{tag}] 后台写入已完成 → {filepath}（{batch} 帧）",
        )
        print(f"Video Batch Processing: [{batch}] frames (queued)", flush=True)
        return [filepath]

    def _save_streaming_append(
        self,
        images: torch.Tensor,
        *,
        workflow_json: str | None,
        safe_prefix: str,
        safe_stream: str,
        batch: int,
        fps: float,
    ) -> list[str]:
        """Streaming：固定单文件 `vates_stream__{stream_id}__{prefix}.dct`；已存在且格式一致则 P2 物理追加。"""
        fname = f"vates_stream__{safe_stream}__{safe_prefix}.dct"
        filepath = os.path.join(self.output_dir, fname)
        x = images.detach()
        if x.device.type != "cpu":
            x = x.cpu()
        if x.dtype != torch.float32:
            x = x.float()
        x = x.contiguous()
        arr = np.ascontiguousarray(x.numpy(), dtype=np.float32)
        if arr.ndim != 4:
            raise ValueError(f"Streaming 需要 BHWC ndim=4，当前 ndim={arr.ndim}")

        tag = f"vates_stream__{safe_stream}__{safe_prefix}"
        prev = vates_core.get_pending_tasks()
        print(f"Vates: [{tag}] saving in background...", flush=True)

        if os.path.isfile(filepath) and _streaming_legacy_p2_would_overwrite(filepath, arr, float(fps)):
            raise RuntimeError(
                "\033[91m[Vates]\033[0m 检测到旧版流式 .dct（无 XXH3 块校验，reserved=1）。"
                "为避免覆盖丢失数据，请先备份或删除/改名该文件，再以 P4 格式重写（reserved=2）。\n"
                f"文件: {filepath}"
            )

        if os.path.isfile(filepath) and _can_append_streaming(filepath, arr, float(fps)):
            vates_core.append_to_vats_async(arr, filepath, float(fps))
        else:
            vates_core.encode_batch_async(
                arr,
                filepath,
                float(fps),
                True,
                SAVE_MODE_TO_ID["Streaming (Append)"],
                workflow_json,
            )

        _after_pending_drops(prev, f"Vates: [{tag}] 后台写入已完成 → {filepath}")
        print(f"[Vates] Streaming 已排队: {filepath}（本批 {batch} 帧）", flush=True)
        return [filepath]

    def _save_image_sequence(
        self,
        images: torch.Tensor,
        *,
        unique: str,
        safe_prefix: str,
        batch: int,
        mode_id: int,
        fps: float,
        workflow_json: str | None,
    ) -> list[str]:
        saved: list[str] = []
        for i in range(batch):
            frame_np = _tensor_bhwc_to_chw_numpy(images, i)
            fname = f"{safe_prefix}_{unique}_{i:05d}.dct"
            filepath = os.path.join(self.output_dir, fname)
            _encode_dct_frame(
                frame_np,
                filepath,
                mode_id=mode_id,
                fps=fps,
                metadata=workflow_json,
            )
            saved.append(filepath)
            print(f"[Vates] 成功保存 DCT 文件 ({i + 1}/{batch}): {filepath}", flush=True)
        return saved

    def save(
        self,
        images: torch.Tensor,
        filename_prefix: str,
        save_mode: str,
        fps: float,
        stream_id: str,
        prompt: object | None = None,
        extra_pnginfo: object | None = None,
    ) -> dict:
        _ensure_vates_loaded()
        batch = _require_bhwc_images(images)
        unique = _make_run_unique()
        safe_prefix = _sanitize_segment(filename_prefix, fallback="vates_tensor")
        safe_stream = _sanitize_segment(stream_id, fallback="default")
        wf = _embed_workflow_json(prompt, extra_pnginfo)

        modes = (
            "Image (Sequence)",
            "Video (Batch)",
            "Streaming (Append)",
        )
        if save_mode not in modes:
            raise ValueError(f"未知 save_mode={save_mode!r}，可选: {modes}")

        mode_id = SAVE_MODE_TO_ID[save_mode]
        fps_f = float(fps)

        if save_mode == "Image (Sequence)":
            saved_paths = self._save_image_sequence(
                images,
                unique=unique,
                safe_prefix=safe_prefix,
                batch=batch,
                mode_id=mode_id,
                fps=fps_f,
                workflow_json=wf,
            )
        elif save_mode == "Video (Batch)":
            saved_paths = self._internal_video_packer(
                images,
                unique=unique,
                safe_prefix=safe_prefix,
                fps=fps_f,
                batch=batch,
                workflow_json=wf,
            )
        else:
            saved_paths = self._save_streaming_append(
                images,
                workflow_json=wf,
                safe_prefix=safe_prefix,
                safe_stream=safe_stream,
                batch=batch,
                fps=fps_f,
            )

        preview = (
            saved_paths[0]
            if len(saved_paths) == 1
            else f"{len(saved_paths)} 个文件（首帧）：{saved_paths[0]}"
        )
        return {"ui": {"text": (preview,)}}


class VatesLoadNode:
    """从 `.dct` 恢复为 ComfyUI IMAGE：单帧为 [1,H,W,C]；P2 多帧为 [B,H,W,C]。

    第二路 `STRING` 为文件内嵌的工作流 JSON（与 Save 时 `prompt`/`extra_pnginfo` 一致打包）；
    无嵌入时为空串，可接其它节点或复制后 **Load (API Format)** 还原画布。
    """

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

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "workflow_json")
    FUNCTION = "load"
    OUTPUT_NODE = False
    CATEGORY = "Vates/IO"

    def load(self, dct_path: str) -> tuple[torch.Tensor, str]:
        _ensure_vates_loaded()

        raw = (dct_path or "").strip()
        if not raw:
            raise ValueError("dct_path 不能为空")

        filepath = raw if os.path.isabs(raw) else os.path.join(self.output_dir, raw)
        filepath = os.path.normpath(filepath)

        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"未找到 DCT 文件: {filepath}")

        try:
            arr, wf_opt = vates_core.decode_tensor_with_workflow(filepath)
        except FileNotFoundError:
            raise
        except Exception as exc:
            msg = str(exc)
            low = msg.lower()
            if "vates_corruption" in low or "data corruption" in low or "xxh3" in low:
                logger.error("Vates 数据损坏（校验失败）: %s — %s", filepath, msg)
                raise RuntimeError(
                    "\033[91m[Vates 数据损坏 / XXH3 校验失败]\033[0m\n"
                    f"路径: {filepath}\n"
                    f"详情: {msg}"
                ) from exc
            if "invalid magic" in low or "bad magic" in low:
                logger.error("Vates 文件头无效（可能非 .dct）: %s", filepath)
                raise RuntimeError(
                    "\033[91m[Vates 文件格式错误]\033[0m 非有效 VATS .dct 或文件已截断。\n"
                    f"路径: {filepath}\n"
                    f"详情: {msg}"
                ) from exc
            raise

        if not isinstance(arr, np.ndarray):
            arr = np.asarray(arr, dtype=np.float32)
        if arr.dtype != np.float32:
            arr = arr.astype(np.float32, copy=False)

        if arr.ndim == 3:
            tensor_chw = torch.from_numpy(np.ascontiguousarray(arr))
            tensor_hwc = tensor_chw.permute(1, 2, 0).contiguous()
            image_bhwc = tensor_hwc.unsqueeze(0)
        elif arr.ndim == 4:
            image_bhwc = torch.from_numpy(np.ascontiguousarray(arr)).permute(0, 2, 3, 1).contiguous()
        else:
            raise ValueError(
                f"解码数组应为 CHW（ndim=3）或 BCHW（ndim=4），当前 ndim={arr.ndim}"
            )

        wf_out = wf_opt if isinstance(wf_opt, str) and wf_opt else ""
        print(f"[Vates] 成功加载 DCT 文件: {filepath}，形状 {tuple(image_bhwc.shape)}", flush=True)
        return (image_bhwc, wf_out)


__all__ = ["VatesLoadNode", "VatesSaveNode"]
