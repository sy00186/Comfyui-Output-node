# Vates（VATeS）

**版本 / Version:** `0.1.1`（与 `Cargo.toml`、`pyproject.toml` 一致）

<p align="center">
  <b>语言切换 Language</b><br>
  <a href="#lang-zh"><b>简体中文</b></a>
  &nbsp;·&nbsp;
  <a href="#lang-en"><b>English</b></a>
</p>

> **使用说明：** 下面分别是「中文」「English」两个可折叠区块。点击标题展开阅读；需切换语言时，折叠当前区块并打开另一区块即可。  
> **How to use:** Two collapsible sections below. Expand the summary line to read; collapse one and open the other to switch language.

---

<a id="lang-zh"></a>

<details open>
<summary><b>📘 简体中文文档（点击标题可折叠）</b></summary>

## Vates — 高性能 `.dct` 张量资产格式

面向 ComfyUI 与通用推理流水线的 **8-bit 量化 + Zstandard 压缩** 方案：固定 **32 字节大端头**、可选 **嵌入 ComfyUI 工作流 JSON**、多帧 **字典压缩** 与 **XXH3 块校验**。

### 目录

1. [二进制协议：32 字节头](#zh-1-二进制协议32-字节头)
2. [性能：零拷贝与并行](#zh-2-性能)
3. [视频字典压缩](#zh-3-视频字典压缩)
4. [可靠性与 XXH3](#zh-4-可靠性与-xxh3)
5. [工作流嵌入与拖拽还原](#zh-5-工作流嵌入与拖拽还原)
6. [ComfyUI 节点](#zh-6-comfyui-节点)
7. [部署与编译](#zh-7-部署与编译)
8. [CLI `vates`](#zh-8-cli-vates)
9. [开发者 SDK](#zh-9-开发者-sdk)
10. [体积对照（示例）](#zh-10-体积对照示例)

---

<h3 id="zh-1-二进制协议32-字节头">1. 二进制协议：32 字节头</h3>

所有合法 `.dct` 文件均以 **32 字节 big-endian 头** 起始，布局见 `src/core.rs` 中 `DctHeader::to_bytes_be` / `from_bytes_be`（`HEADER_LEN = 32`）。

| 偏移 | 大小 | 字段 | 说明 |
|------|------|------|------|
| 0–3 | 4 | Magic | 固定 **`VATS`** |
| 4–5 | 2 | Version | `u16` BE，当前 **`1`**，否则拒绝解码 |
| 6 | 1 | Mode | `u8`：ComfyUI 约定 `0` 图像序列 / `1` 视频批 / `2` 流式追加 |
| 7 | 1 | Reserved | 低 7 位 = **容器类型**；**`0x80`** = 头后存在 **`META` 工作流块** |
| 8–11 | 4 | Batch (B) | `u32` BE |
| 12–15 | 4 | Channels (C) | `u32` BE |
| 16–19 | 4 | Height (H) | `u32` BE |
| 20–23 | 4 | Width (W) | `u32` BE |
| 24–27 | 4 | FPS | **`f32` big-endian** |
| 28–31 | 4 | Padding | 当前为 **0**（保留） |

**`Reserved` 低 7 位（容器）：** `0` = 单段 Zstd；`1` = 多帧多块（历史，无每块 XXH3）；`2` = 多帧 + 每块后 **`u64` XXH3-64**（多帧默认）。**`0x80`** 为 1 时，头后顺序为：**`META`** + `u32` BE（Zstd 长度）+ **zstd(UTF-8 JSON)**；否则不写该块。

**主载荷概要：** 单帧容器在头（及可选 META）后为 **一段 Zstd**，解压得 `min f32 LE`、`max f32 LE`、**CHW `u8` 量化**。多帧：字典长度 + 字典 + 块数 + 每帧（块长 + Zstd + 可选 XXH3）。

---

<h3 id="zh-2-性能">2. 性能：零拷贝、并行量化、多线程 Zstd</h3>

- **零拷贝（Python → Rust）：** `encode_tensor` 要求 **C 连续 `float32` CHW** NumPy；PyO3 合法布局下直接借用缓冲区，`ArrayView1` 编码。否则请先 `numpy.ascontiguousarray(..., dtype=float32)`。
- **并行量化：** min/max 与 8-bit 量化使用 **Rayon**（并行规约与逐元素写回）。
- **多线程 Zstd：** 依赖启用 **`zstdmt`**；`zstd_encode_mt` 按 `available_parallelism()` 设置 worker（**上限 8**）。

---

<h3 id="zh-3-视频字典压缩">3. 视频字典压缩</h3>

多帧 **BHWC** 写入时，自最多前 **32** 帧训练 **Zstd 字典**（上限约 **64 KiB**），后续帧用 **`EncoderDictionary`** 压缩，通常远小于等量 **逐帧 PNG 序列** 总体积，且为 **单文件**。

---

<h3 id="zh-4-可靠性与-xxh3">4. 可靠性与 XXH3</h3>

**`reserved` 低 7 位 = `2`** 时，每块压缩数据后附 **`u64` BE** 的 **XXH3-64**（对压缩字节计算）。不匹配则 **`DctError::DataCorruption`**（Python 侧常见 `ValueError`，含 `VATES_CORRUPTION`）。

---

<h3 id="zh-5-工作流嵌入与拖拽还原">5. 工作流资产化：嵌入与拖拽还原</h3>

- **保存：** `VatesSaveNode` 通过 **`hidden`** 的 `prompt`、`extra_pnginfo` 序列化 JSON，经 **`metadata`** 写入 **`META` + zstd(JSON)**。
- **加载：** `VatesLoadNode` 输出 **`IMAGE`** 与 **`workflow_json`**（`decode_tensor_with_workflow`）。
- **拖拽：** `web/vates_dct_drop.js` + `POST /vates/extract_workflow`（`vates_server_hooks.py`）上传 `.dct`，服务端 `read_embedded_workflow_json`，前端 `loadGraphData` 等还原画布。

---

<h3 id="zh-6-comfyui-节点">6. ComfyUI 节点（`vates_nodes.py`）</h3>

**`VatesSaveNode`**（`OUTPUT_NODE = True`）  
必填：`images`（**`IMAGE`** `[B,H,W,C]`）、`filename_prefix`、`save_mode`、`fps`、`stream_id`。  
隐藏：`prompt` = `PROMPT`，`extra_pnginfo` = `EXTRA_PNGINFO`。  
`save_mode`：`Image (Sequence)` / `Video (Batch)` / `Streaming (Append)` ↔ 头 **`mode`** `0` / `1` / `2`。

**`VatesLoadNode`**  
必填：`dct_path`（**`STRING`**）。  
`RETURN_TYPES`：`IMAGE`, `STRING`；`RETURN_NAMES`：`image`, `workflow_json`。

---

<h3 id="zh-7-部署与编译">7. 部署与编译</h3>

**环境：** Rust **1.70+**，Python **3.9+**，`cargo`；扩展推荐 **maturin**；ComfyUI 需 **torch**、**numpy**、**folder_paths**。

**仅 CLI / 纯 Rust（无 Python 模块）：**

```bash
cd dct-core
cargo build --release --no-default-features
```

**Python 扩展：**

```bash
maturin develop --release
```

**Linux：** 若产物为 `libvates_core.so` 而运行时需要 `vates_core.so`：

```bash
cp target/release/libvates_core.so ./vates_core.so
```

将 **`custom_nodes/ComfyUI-Vates`** 或整包 **`dct-core`** 链入 ComfyUI；可用 **`install.py`** 或 **`wheels/`**。

---

<h3 id="zh-8-cli-vates">8. CLI `vates`</h3>

```bash
cargo run --release --no-default-features --bin vates -- inspect path/to/file.dct
cargo run --release --no-default-features --bin vates -- inspect --json path/to/file.dct
cargo run --release --no-default-features --bin vates -- verify path/to/file.dct
```

---

<h3 id="zh-9-开发者-sdk">9. 开发者 SDK 示例</h3>

**Python：**

```python
import numpy as np
import vates_core as vc

chw = np.ascontiguousarray(np.random.rand(3, 64, 64).astype(np.float32))
vc.encode_tensor(chw, "out.dct", mode=0, fps=24.0, batch=1, metadata=None)
arr, wf = vc.decode_tensor_with_workflow("out.dct")
meta = vc.read_embedded_workflow_json("out.dct")

bhwc = np.ascontiguousarray(np.random.rand(10, 64, 64, 3).astype(np.float32))
vc.encode_batch(bhwc, "video.dct", fps=24.0, force_p2=False, header_mode=1, metadata=None)
```

`peek_dct_header(path)` → `(batch, channels, height, width, mode, reserved, fps)`。

**Rust（`default-features = false`）：**

```rust
use vates_core::Decoder;
let decoded = Decoder::decode_file_full("file.dct")?;
```

---

<h3 id="zh-10-体积对照示例">10. 体积与速度对照（示意，非承诺）</h3>

场景：**1024×1024**，RGB，**64** 帧。请以实测为准。

| 指标 | PNG 序列（示意） | 单 `.dct`（字典 + XXH3，示意） |
|------|------------------|--------------------------------|
| 总体积 | 约 40–120 MB | 约 8–35 MB |
| 原始 FP32 | — | 约 768 MB |
| 编码 | 常较慢 | 并行量化 + zstd 多线程，常更快 |

---

### 仓库布局（中文）

```
dct-core/
  src/core.rs, src/python_binding.rs, src/main.rs
  vates_nodes.py, vates_server_hooks.py, web/vates_dct_drop.js
  custom_nodes/ComfyUI-Vates/, install.py, check_vates.py
```

**许可：** MIT OR Apache-2.0

</details>

---

<a id="lang-en"></a>

<details>
<summary><b>📗 English documentation (click to expand)</b></summary>

## Vates — High-performance `.dct` tensor asset format

**8-bit quantization + Zstandard** for ComfyUI and general pipelines: **32-byte big-endian header**, optional **embedded ComfyUI workflow JSON**, multi-frame **dictionary compression**, and **per-block XXH3** integrity checks.

### Table of contents

1. [Binary header (32 bytes)](#en-1-binary-header-32-bytes)
2. [Performance](#en-2-performance)
3. [Dictionary compression](#en-3-dictionary-compression)
4. [Integrity (XXH3)](#en-4-integrity-xxh3)
5. [Workflow embedding & drag-restore](#en-5-workflow-embedding--drag-restore)
6. [ComfyUI nodes](#en-6-comfyui-nodes)
7. [Build & deployment](#en-7-build--deployment)
8. [CLI `vates`](#en-8-cli-vates)
9. [SDK examples](#en-9-sdk-examples)
10. [Size benchmark (illustrative)](#en-10-size-benchmark-illustrative)

---

<h3 id="en-1-binary-header-32-bytes">1. Binary header (32 bytes)</h3>

Every `.dct` file starts with a **32-byte big-endian** header (`DctHeader` in `src/core.rs`, `HEADER_LEN = 32`).

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0–3 | 4 | Magic | ASCII **`VATS`** |
| 4–5 | 2 | Version | `u16` BE, must be **`1`** |
| 6 | 1 | Mode | `u8`: `0` image sequence / `1` video batch / `2` streaming append |
| 7 | 1 | Reserved | Low 7 bits = **container**; bit **`0x80`** = **META** workflow block after header |
| 8–11 | 4 | Batch (B) | `u32` BE |
| 12–15 | 4 | Channels (C) | `u32` BE |
| 16–19 | 4 | Height (H) | `u32` BE |
| 20–23 | 4 | Width (W) | `u32` BE |
| 24–27 | 4 | FPS | **`f32` big-endian** |
| 28–31 | 4 | Padding | **zeros** (reserved) |

**Container (low 7 bits):** `0` single zstd segment; `1` multi-block legacy (no per-block hash); `2` multi-block + **`u64` XXH3-64** after each compressed block (default for new multi-frame writes).

**Optional workflow block** when **`reserved & 0x80`:** **`META`** + big-endian **`u32`** (compressed length) + **zstd(UTF-8 JSON)**.

**Payload overview:** After optional META: single zstd → `min f32 LE`, `max f32 LE`, **CHW `u8`**; or multi-frame **dictionary** + per-frame blocks + optional XXH3.

---

<h3 id="en-2-performance">2. Performance: zero-copy, parallel quantization, threaded Zstd</h3>

- **Zero-copy:** `encode_tensor` takes **contiguous `float32` CHW** NumPy; PyO3 borrows the buffer and Rust uses `ArrayView1`. Use `numpy.ascontiguousarray(..., dtype=float32)` if needed.
- **Parallel quantization:** **Rayon** for min/max and 8-bit quantization.
- **Multi-threaded Zstd:** **`zstdmt`** feature; encoder uses up to **8** worker threads in `zstd_encode_mt`.

---

<h3 id="en-3-dictionary-compression">3. Dictionary compression (multi-frame)</h3>

Up to **32** training frames build a shared **Zstd dictionary** (capped around **64 KiB**); frames are compressed with **`EncoderDictionary`** when beneficial—usually much smaller total size than a **PNG sequence**, single file.

---

<h3 id="en-4-integrity-xxh3">4. Integrity (XXH3)</h3>

For container **`2`**, each compressed block is followed by **`u64` BE** **XXH3-64** of the **compressed** bytes. Mismatch → **`DctError::DataCorruption`** (often `ValueError` with `VATES_CORRUPTION` in Python).

---

<h3 id="en-5-workflow-embedding--drag-restore">5. Workflow embedding & drag-restore</h3>

- **Save:** `VatesSaveNode` reads **`hidden`** `prompt` / `extra_pnginfo`, JSON → **`metadata`** → **`META` + zstd(JSON)**.
- **Load:** `VatesLoadNode` → **`IMAGE`** + **`workflow_json`** via **`decode_tensor_with_workflow`**.
- **Drag-drop:** **`web/vates_dct_drop.js`**, **`POST /vates/extract_workflow`** in **`vates_server_hooks.py`**, **`read_embedded_workflow_json`**, **`app.loadGraphData`** (or API import fallbacks).

---

<h3 id="en-6-comfyui-nodes">6. ComfyUI nodes (`vates_nodes.py`)</h3>

**`VatesSaveNode`** (`OUTPUT_NODE = True`): required **`images`** (`IMAGE` BHWC), `filename_prefix`, `save_mode`, `fps`, `stream_id`; hidden **`prompt`**, **`extra_pnginfo`**. Modes map to header **`mode`** `0` / `1` / `2`.

**`VatesLoadNode`:** **`dct_path`** (`STRING`); **`RETURN_TYPES`**: `IMAGE`, `STRING`; **`RETURN_NAMES`**: `image`, `workflow_json`.

---

<h3 id="en-7-build--deployment">7. Build & deployment</h3>

**Prerequisites:** Rust **1.70+**, Python **3.9+**, **maturin** recommended for the extension; ComfyUI needs **torch**, **numpy**, **folder_paths**.

**CLI / Rust only (no PyO3 module):**

```bash
cd dct-core
cargo build --release --no-default-features
```

**Python extension:**

```bash
maturin develop --release
```

**Linux:** if build outputs **`libvates_core.so`** but runtime expects **`vates_core.so`:**

```bash
cp target/release/libvates_core.so ./vates_core.so
```

Install **`ComfyUI-Vates`** or the full **`dct-core`** tree as a custom node; use **`install.py`** or wheels under **`wheels/`**.

---

<h3 id="en-8-cli-vates">8. CLI `vates`</h3>

```bash
cargo run --release --no-default-features --bin vates -- inspect path/to/file.dct
cargo run --release --no-default-features --bin vates -- inspect --json path/to/file.dct
cargo run --release --no-default-features --bin vates -- verify path/to/file.dct
```

---

<h3 id="en-9-sdk-examples">9. SDK examples</h3>

**Python:**

```python
import numpy as np
import vates_core as vc

chw = np.ascontiguousarray(np.random.rand(3, 64, 64).astype(np.float32))
vc.encode_tensor(chw, "out.dct", mode=0, fps=24.0, batch=1, metadata=None)
arr, wf = vc.decode_tensor_with_workflow("out.dct")

bhwc = np.ascontiguousarray(np.random.rand(10, 64, 64, 3).astype(np.float32))
vc.encode_batch(bhwc, "video.dct", fps=24.0, force_p2=False, header_mode=1, metadata=None)
```

`peek_dct_header(path)` → `(batch, channels, height, width, mode, reserved, fps)`.

**Rust:**

```toml
[dependencies]
vates_core = { path = "../dct-core", default-features = false }
```

```rust
use vates_core::Decoder;
let decoded = Decoder::decode_file_full("file.dct")?;
let _ = Decoder::verify_file("file.dct")?;
```

---

<h3 id="en-10-size-benchmark-illustrative">10. Size vs PNG (illustrative, not a benchmark guarantee)</h3>

**1024×1024**, RGB, **64** frames. Measure on your own hardware.

| Metric | PNG sequence (illustrative) | Single `.dct` (dict + XXH3, illustrative) |
|--------|-----------------------------|---------------------------------------------|
| Total size | ~40–120 MB | ~8–35 MB |
| Raw FP32 | — | ~768 MB |
| Encode | often slower | parallel quant + zstd MT, often faster |

---

### Repository layout

```
dct-core/
  src/core.rs, src/python_binding.rs, src/main.rs
  vates_nodes.py, vates_server_hooks.py, web/vates_dct_drop.js
  custom_nodes/ComfyUI-Vates/, install.py, check_vates.py
```

**License:** MIT OR Apache-2.0

</details>
