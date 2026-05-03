"""
Vates：ComfyUI 节点（保存 / 加载 .dct）。

- 调试计时（纳秒）：设置环境变量 **`VATES_ENCODE_TIMING=1`**（Rust 侧 `encode_tensor` 与 Python `_encode_dct_frame` 均会输出墙钟）。
- ComfyUI IMAGE 为 torch.Tensor，形状 [Batch, Height, Width, Channels]（BHWC）。
- Rust 扩展 vates_core.encode_tensor：CHW float32 C 连续；并写入 32B VATS 头字段 mode、fps、B/C/H/W（详见 Rust 文档）。
"""

from __future__ import annotations

import atexit
import glob
import json
import logging
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import folder_paths
import numpy as np
import torch
from PIL import Image

from .vates_repo_meta import expected_vates_core_version

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

# 与 pyproject.toml / Cargo 发布的 crate 版本一致（对照 vates_core.__version__）
VATES_EXPECTED_CORE_VERSION: str = expected_vates_core_version()


def _vates_repo_root() -> Path:
    """含 `vates_nodes.py` 与对齐后的 `vates_core` 二进制的目录。"""
    return Path(__file__).resolve().parent


def _pick_windows_native_artifact(release_dir: Path) -> Path | None:
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


def _try_align_native_from_target_release(root: Path) -> list[str]:
    """若 `target/release` 存在典型产物，则复制到根目录以便 `import vates_core`。返回已写入路径。"""
    release = root / "target" / "release"
    touched: list[str] = []
    if not release.is_dir():
        return touched

    if sys.platform == "win32":
        src = _pick_windows_native_artifact(release)
        if src is not None:
            dst = root / "vates_core.pyd"
            shutil.copy2(src, dst)
            touched.append(str(dst))
    elif sys.platform == "darwin":
        dylib = release / "libvates_core.dylib"
        if dylib.is_file():
            dst = root / "vates_core.so"
            shutil.copy2(dylib, dst)
            try:
                os.chmod(
                    dst,
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
            touched.append(str(dst))
        else:
            lib = release / "libvates_core.so"
            alt = release / "vates_core.so"
            if lib.is_file():
                dst = root / "vates_core.so"
                shutil.copy2(lib, dst)
                try:
                    os.chmod(
                        dst,
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
                touched.append(str(dst))
            elif alt.is_file():
                dst = root / "vates_core.so"
                shutil.copy2(alt, dst)
                try:
                    os.chmod(
                        dst,
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
                touched.append(str(dst))
    else:
        lib = release / "libvates_core.so"
        alt = release / "vates_core.so"
        if lib.is_file():
            dst = root / "vates_core.so"
            shutil.copy2(lib, dst)
            try:
                os.chmod(
                    dst,
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
            touched.append(str(dst))
        elif alt.is_file():
            dst = root / "vates_core.so"
            shutil.copy2(alt, dst)
            try:
                os.chmod(
                    dst,
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
            touched.append(str(dst))
    return touched


def _format_vates_import_error(
    cause: BaseException,
    *,
    root: Path,
    tried_paths: list[str],
) -> str:
    cwd = os.getcwd()
    release = root / "target" / "release"
    lines = [
        "未能导入 vates_core（原生扩展未就位或未对齐）。",
        f"  当前工作目录 (cwd): {cwd}",
        f"  插件/代码根目录 (Vates root): {root}",
        "",
        "  已检查或可供对齐的路径（含 target/release 典型产物）:",
    ]
    for p in tried_paths:
        exists = "存在" if Path(p).is_file() else "不存在"
        lines.append(f"    [{exists}] {p}")
    if not tried_paths:
        lines.append("    （无）")
    lines.extend(
        [
            "",
            "  若 `target/release` 下已有 libvates_core.so / libvates_core.dylib / vates_core.dll，",
            "  但未自动导入成功，请确认已在 **含 Cargo.toml 的仓库根** 运行 `python install.py` 完成对齐；",
            "  若 ComfyUI 加载的是 **其它目录** 下的 vates_nodes.py，请设置环境变量 VATES_POST_COPY_DIR 为该目录后重装。",
            "",
            "  解决办法：在 Vates 仓库根目录（含 Cargo.toml 与 install.py）执行一次：",
            "    python install.py",
            "  然后重启 ComfyUI。",
            f"  详情: {cause}",
        ]
    )
    return "\n".join(lines)


def _verify_vates_core_version(vc: object) -> None:
    """原生扩展与节点代码版本须一致，避免 ABI/协议漂移导致崩溃。"""
    got = getattr(vc, "__version__", None)
    if got != VATES_EXPECTED_CORE_VERSION:
        raise RuntimeError(
            "vates_core 与当前节点代码版本不匹配："
            f"扩展报告 __version__={got!r}，节点期望 {VATES_EXPECTED_CORE_VERSION!r}。"
            "请在本仓库根目录重新执行 `python install.py` 或安装对应版本的 wheel，然后重启 ComfyUI。"
        )


def _ensure_vates_loaded() -> None:
    """确认原生扩展 vates_core 已载入（惰性导入；必要时从 target/release 对齐到仓库根目录）。"""
    global vates_core
    if vates_core is not None:
        return
    root = _vates_repo_root()
    current_dir = str(root)
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

    release = root / "target" / "release"
    tried_paths: list[str] = [
        str(root / "vates_core.so"),
        str(root / "vates_core.pyd"),
        str(release / "libvates_core.dylib"),
        str(release / "libvates_core.so"),
        str(release / "vates_core.so"),
        str(release / "vates_core.dll"),
    ]
    tried_paths.extend(sorted(glob.glob(str(release / "vates_core*.pyd"))))

    def _do_import() -> object:
        import vates_core as vc

        return vc

    try:
        vates_core = _do_import()
    except (ImportError, OSError):
        aligned = _try_align_native_from_target_release(root)
        if aligned:
            tried_paths.extend(aligned)
        try:
            vates_core = _do_import()
        except (ImportError, OSError) as e2:
            raise RuntimeError(_format_vates_import_error(e2, root=root, tried_paths=tried_paths)) from e2
    except Exception as e:
        raise RuntimeError(_format_vates_import_error(e, root=root, tried_paths=tried_paths)) from e

    _verify_vates_core_version(vates_core)
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


def _resolved_path_under_output(raw: str, output_dir: str) -> str:
    """将 ``dct_path`` 规范为绝对路径并校验落在 ``output_dir`` 沙箱内（防绝对路径与 ``..`` 逃逸）。"""
    out = (output_dir or "").strip()
    if not out:
        raise ValueError("ComfyUI output 目录无效")
    out_abs = os.path.realpath(out)
    s = (raw or "").strip()
    if not s:
        raise ValueError("dct_path 不能为空")
    cand = os.path.realpath(s) if os.path.isabs(s) else os.path.realpath(os.path.join(out_abs, s))
    try:
        common = os.path.commonpath([cand, out_abs])
    except ValueError as exc:
        raise PermissionError(
            f"[Vates] dct_path 拒绝访问：解析结果 {cand!r} 与授权 output 根 {out_abs!r} 无法建立共同路径前缀（例如跨盘符）。"
        ) from exc
    if os.path.normcase(common) != os.path.normcase(out_abs):
        raise PermissionError(
            f"[Vates] dct_path 已解析为 {cand!r}，不在 ComfyUI output 目录 {out_abs!r} 内。"
        )
    return cand


def _list_dct_rel_paths_under_output(output_dir: str) -> list[str]:
    """递归列出 ``output`` 下所有 ``.dct``，返回相对 ``output_dir`` 的路径（POSIX 斜杠）。"""
    out = (output_dir or "").strip()
    if not out or not os.path.isdir(out):
        return []
    out_abs = os.path.realpath(out)
    found: list[str] = []
    for root, _dirs, files in os.walk(out_abs):
        for f in files:
            if not f.lower().endswith(".dct"):
                continue
            full = os.path.join(root, f)
            try:
                rel = os.path.relpath(full, out_abs)
            except ValueError:
                continue
            if rel.startswith(".." + os.sep) or rel == "..":
                continue
            found.append(rel.replace("\\", "/"))
    return sorted(found)


def _require_path_under_comfy_output(filepath: str, output_dir: str) -> str:
    """保存链路生成的路径亦经 ``realpath`` + ``commonpath`` 校验，防止异常配置下的逃逸。"""
    out = (output_dir or "").strip()
    if not out:
        raise ValueError("ComfyUI output 目录无效")
    out_abs = os.path.realpath(out)
    cand = os.path.realpath(filepath)
    try:
        common = os.path.commonpath([cand, out_abs])
    except ValueError as exc:
        raise PermissionError(
            f"[Vates] 写入路径 {cand!r} 已越权：与授权 output 根 {out_abs!r} 无法建立共同前缀。"
        ) from exc
    if os.path.normcase(common) != os.path.normcase(out_abs):
        raise PermissionError(
            f"[Vates] 写入路径 {cand!r} 不在 ComfyUI output 目录 {out_abs!r} 内。"
        )
    return cand


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
    try:
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
    except Exception:
        logger.exception("Vates: encode_tensor 失败 path=%s", filepath)
        raise


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


def _can_append_streaming(
    filepath: str, arr_bhwc: np.ndarray, fps: float, output_dir: str
) -> bool:
    """已与盘上 P2+XXH3 Stream 文件对齐（C/H/W、fps、mode、reserved=2）则可 append。"""
    try:
        filepath = _require_path_under_comfy_output(filepath, output_dir)
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
    filepath: str, arr_bhwc: np.ndarray, fps: float, output_dir: str
) -> bool:
    """旧版 P2（无 XXH3）流式同名文件：若直接 encode 会截断覆盖，需用户先处理。"""
    try:
        filepath = _require_path_under_comfy_output(filepath, output_dir)
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

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "Vates/IO"

    @classmethod
    def IS_CHANGED(  # noqa: N802 — ComfyUI 协议名
        cls,
        images: object,
        filename_prefix: str,
        save_mode: str,
        fps: float,
        stream_id: str,
        prompt: object | None = None,
        extra_pnginfo: object | None = None,
    ) -> float:
        """输出节点每次执行均参与刷新，避免默认缓存导致导出行为歧义。"""
        return float("nan")

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
        filepath = _require_path_under_comfy_output(filepath, self.output_dir)
        tag = f"{safe_prefix}_vbatch_{unique}"
        prev = vates_core.get_pending_tasks()
        print(f"Vates: [{tag}] saving in background...", flush=True)
        try:
            vates_core.encode_batch_async(
                arr,
                filepath,
                float(fps),
                False,
                SAVE_MODE_TO_ID["Video (Batch)"],
                workflow_json,
            )
        except Exception:
            logger.exception(
                "Vates: encode_batch_async（Video Batch）调度失败 path=%s", filepath
            )
            raise
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

        filepath = _require_path_under_comfy_output(filepath, self.output_dir)
        tag = f"vates_stream__{safe_stream}__{safe_prefix}"
        prev = vates_core.get_pending_tasks()
        print(f"Vates: [{tag}] saving in background...", flush=True)

        if os.path.isfile(filepath) and _streaming_legacy_p2_would_overwrite(
            filepath, arr, float(fps), self.output_dir
        ):
            raise RuntimeError(
                "\033[91m[Vates]\033[0m 检测到旧版流式 .dct（无 XXH3 块校验，reserved=1）。"
                "为避免覆盖丢失数据，请先备份或删除/改名该文件，再以 P4 格式重写（reserved=2）。\n"
                f"文件: {filepath}"
            )

        try:
            if os.path.isfile(filepath) and _can_append_streaming(
                filepath, arr, float(fps), self.output_dir
            ):
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
        except Exception:
            logger.exception("Vates: Streaming 写入调度失败 path=%s", filepath)
            raise

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
            filepath = _require_path_under_comfy_output(
                os.path.join(self.output_dir, fname), self.output_dir
            )
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
        return {
            "ui": {"text": (preview,)},
            "result": (images,),
        }


class VatesLoadAndPreview:
    """自 ComfyUI ``output`` 目录选择 ``.dct``：全精度 ``IMAGE`` 供下游，并写临时 PNG 做 UI 预览（首帧）。"""

    def __init__(self) -> None:
        self.output_dir = folder_paths.get_output_directory()
        self.temp_dir = folder_paths.get_temp_directory()
        self._type = "temp"

    @classmethod
    def INPUT_TYPES(cls) -> dict:  # noqa: N802
        out = folder_paths.get_output_directory()
        names = _list_dct_rel_paths_under_output(out)
        choices = names if names else ["(output 目录下暂无 .dct 文件)"]
        default = names[0] if names else choices[0]
        return {
            "required": {
                "dct_file": (
                    tuple(choices),
                    {
                        "default": default,
                        "tooltip": "扫描自 folder_paths.get_output_directory()，选中的为相对 output 的路径",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "load_and_preview"
    OUTPUT_NODE = True
    CATEGORY = "Vates/IO"

    @classmethod
    def IS_CHANGED(  # noqa: N802
        cls,
        dct_file: str,
    ) -> float:
        return float("nan")

    def load_and_preview(self, dct_file: str) -> dict:
        _ensure_vates_loaded()

        if not dct_file or dct_file.startswith("("):
            raise FileNotFoundError("请先在 output 目录保存或放入至少一个 .dct，再于下拉列表中选择。")

        filepath = _resolved_path_under_output(dct_file, self.output_dir)
        if not os.path.isfile(filepath):
            raise FileNotFoundError(f"未找到 DCT 文件: {filepath}")

        try:
            arr, _wf_opt = vates_core.decode_tensor_with_workflow(filepath)
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
            logger.exception("Vates: decode_tensor_with_workflow 失败 path=%s", filepath)
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

        # 明线预览：仅首帧，0..1 → 0..255 uint8，供 PIL；不改动 image_bhwc（下游全精度）。
        frame = image_bhwc[0].detach().cpu().numpy().astype(np.float32, copy=False)
        clipped = np.clip(frame, 0.0, 1.0)
        rgb_u8 = np.clip(np.round(clipped * 255.0), 0.0, 255.0).astype(np.uint8)
        c = rgb_u8.shape[2]
        if c == 1:
            pil_img = Image.fromarray(rgb_u8[:, :, 0], mode="L")
        elif c == 3:
            pil_img = Image.fromarray(rgb_u8, mode="RGB")
        elif c == 4:
            pil_img = Image.fromarray(rgb_u8, mode="RGBA")
        else:
            pil_img = Image.fromarray(rgb_u8[:, :, :3], mode="RGB")

        prefix = f"vates_preview_{secrets.token_hex(8)}"
        fname = f"{prefix}.png"
        temp_base = os.path.abspath(self.temp_dir)
        if not os.path.exists(temp_base):
            os.makedirs(temp_base, exist_ok=True)
        dest = os.path.abspath(os.path.join(temp_base, fname))
        pil_img.save(dest, format="PNG", compress_level=1)

        print(
            f"[Vates] Load & Preview: {filepath}，形状 {tuple(image_bhwc.shape)} → 预览 {fname}",
            flush=True,
        )
        return {
            "result": (image_bhwc,),
            "ui": {
                "images": [
                    {
                        "filename": fname,
                        "subfolder": "",
                        "type": self._type,
                    }
                ]
            },
        }


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

        filepath = _resolved_path_under_output(dct_path, self.output_dir)

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
            logger.exception("Vates: decode_tensor_with_workflow 失败 path=%s", filepath)
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


__all__ = ["VatesLoadAndPreview", "VatesLoadNode", "VatesSaveNode"]
