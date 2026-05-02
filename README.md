# Vates

**Version:** `0.1.1` — matches `Cargo.toml` and `pyproject.toml`.

<p align="center">
  <b>Documentation</b><br>
  <a href="#lang-en"><b>English</b></a>
  &nbsp;·&nbsp;
  <a href="#lang-zh"><b>Chinese (Simplified)</b></a>
</p>

> **i18n layout:** The **English** guide is listed **first** and opens by default. **Chinese (Simplified)** follows—expand that panel when you need it. Collapse either panel to reduce scrolling.

---

<a id="lang-en"></a>

<details open>
<summary><b>📗 English — full documentation (shown by default; click to collapse)</b></summary>

## Vates — High-performance `.dct` tensor asset format

Vates stores **8-bit-quantized** tensors under a **fixed 32-byte, big-endian header**, compresses them with **Zstandard**, and optionally **embeds ComfyUI workflow JSON** inside the same `.dct` file. Multi-frame clips use a **trained Zstd dictionary**; current writers also attach a **per-block XXH3-64** checksum for tamper and corruption detection.

### Table of contents

1. [Binary header (32 bytes)](#en-1-binary-header-32-bytes)
2. [Performance](#en-2-performance)
3. [Dictionary compression](#en-3-dictionary-compression)
4. [Integrity (XXH3)](#en-4-integrity-xxh3)
5. [Workflow embedding and drag-and-drop restore](#en-5-workflow-embedding--drag-restore)
6. [ComfyUI nodes](#en-6-comfyui-nodes)
7. [Build and deployment](#en-7-build--deployment)
8. [Command-line tool (`vates`)](#en-8-cli-vates)
9. [SDK examples](#en-9-sdk-examples)
10. [Size versus PNG (illustrative)](#en-10-size-benchmark-illustrative)

---

<h3 id="en-1-binary-header-32-bytes">1. Binary header (32 bytes)</h3>

Every valid `.dct` file begins with a **32-byte big-endian header**. The exact layout is implemented as `DctHeader::to_bytes_be` / `from_bytes_be` in `src/core.rs` (`HEADER_LEN = 32`).

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0–3 | 4 | Magic | Literal ASCII **`VATS`**. |
| 4–5 | 2 | Version | Unsigned 16-bit **big-endian**. Must be **`1`**; other values are rejected. |
| 6 | 1 | Mode | Unsigned 8-bit semantic tag used with ComfyUI: **`0`** image sequence, **`1`** video batch, **`2`** streaming append. |
| 7 | 1 | Reserved | Lower **7 bits** select the **container** layout; bit **`0x80`** means a **`META`** workflow block follows the header. |
| 8–11 | 4 | Batch (B) | Unsigned 32-bit **big-endian** frame count. |
| 12–15 | 4 | Channels (C) | Unsigned 32-bit **big-endian** (e.g. **3** for RGB). |
| 16–19 | 4 | Height (H) | Unsigned 32-bit **big-endian**. |
| 20–23 | 4 | Width (W) | Unsigned 32-bit **big-endian**. |
| 24–27 | 4 | FPS | **32-bit floating-point, big-endian** (IEEE-754). |
| 28–31 | 4 | Padding | **Zero** in the current format (reserved for future use). |

**Container type (lower seven bits of `reserved`).**  
**`0`:** one contiguous Zstd payload after the header (typical single-frame layout).  
**`1`:** legacy multi-block container (no per-block digest).  
**`2`:** multi-block container where each compressed block is followed by a **`u64` big-endian XXH3-64** value; this is what new multi-frame encodes use by default.

**Optional workflow metadata (when bit `0x80` is set).** Immediately after the 32-byte header: the ASCII tag **`META`**, a **big-endian `u32`** giving the length of the following Zstd blob, then **Zstd-compressed UTF-8 JSON** (usually ComfyUI `prompt` / `extra_pnginfo` serialized from Python). If the bit is clear, this block is omitted and the file stays backward-compatible with headers that carry no metadata.

**Payload layout (summary).**  
After the header and optional `META` block, a **single-frame** file holds one Zstd segment whose decompressed payload is **`min` (f32, LE)**, **`max` (f32, LE)**, then **CHW-ordered `u8`** samples. **Multi-frame** files append a dictionary region, a block count, then per-frame **compressed length + Zstd bytes + (for type `2`) XXH3**.

---

<h3 id="en-2-performance">2. Performance: zero-copy path, parallel quantization, threaded Zstd</h3>

- **Zero-copy from Python.** The `encode_tensor` binding expects a **C-contiguous `float32`** NumPy array in **CHW** order. When the layout is valid, PyO3 hands Rust a read-only view and the encoder wraps it as an `ArrayView1` without copying the pixel buffer first. Call `numpy.ascontiguousarray(..., dtype=numpy.float32)` if the binding reports a layout error.

- **Parallel quantization.** Global min/max and the linear map to **8-bit** codes use **Rayon**, so large frames benefit from multiple CPU cores.

- **Multi-threaded compression.** The crate enables Zstandard’s **`zstdmt`** integration. The helper `zstd_encode_mt` caps worker count at **eight** threads derived from `std::thread::available_parallelism()`, which improves throughput on large uncompressed buffers.

---

<h3 id="en-3-dictionary-compression">3. Dictionary compression (multi-frame)</h3>

When you encode a **BHWC** batch, the implementation samples up to the **first 32 frames**, builds a **shared Zstd dictionary** (bounded by about **64 KiB**), and compresses subsequent frames with that dictionary when it helps. The result is usually **much smaller on disk than an equivalent sequence of PNG files**, and everything lives in **one** `.dct` asset.

---

<h3 id="en-4-integrity-xxh3">4. Integrity (XXH3)</h3>

For container type **`2`**, each compressed frame is stored as **compressed bytes** followed by **`u64` big-endian XXH3-64** of those same bytes. On read or verify, if the hash does not match, the decoder returns **`DctError::DataCorruption`**. In Python you will typically see a **`ValueError`** whose message includes **`VATES_CORRUPTION`**.

---

<h3 id="en-5-workflow-embedding--drag-restore">5. Workflow embedding and drag-and-drop restore</h3>

- **Saving.** `VatesSaveNode` receives ComfyUI’s **`hidden`** inputs **`prompt`** and **`extra_pnginfo`**, serializes them to JSON, and passes that string as **`metadata`** into the Rust encoder, which may write **`META` + Zstd(JSON)** after the header.

- **Loading.** `VatesLoadNode` exposes **`IMAGE`** plus a **`STRING`** output **`workflow_json`** by calling **`decode_tensor_with_workflow`**. If nothing was embedded, that string is empty.

- **Drag-and-drop in the browser.** The script **`web/vates_dct_drop.js`** listens for **`.dct`** files dropped on the page, uploads them to **`POST /vates/extract_workflow`** (multipart field **`file`**), which is registered in **`vates_server_hooks.py`**. The server calls **`read_embedded_workflow_json`**. The front end then tries to restore the graph with **`app.loadGraphData`**, or falls back to API-format import helpers when available.

---

<h3 id="en-6-comfyui-nodes">6. ComfyUI nodes (`vates_nodes.py`)</h3>

**`VatesSaveNode`** (`OUTPUT_NODE = True`)  

- **Required widget inputs:** `images` (**`IMAGE`**, shape **`[B, H, W, C]`**), `filename_prefix`, `save_mode`, `fps`, `stream_id`.  
- **Hidden inputs (filled in by ComfyUI):** `prompt` bound to **`PROMPT`**, `extra_pnginfo` bound to **`EXTRA_PNGINFO`**.  
- **`save_mode` values** `Image (Sequence)`, `Video (Batch)`, and `Streaming (Append)` set header **`mode`** to **`0`**, **`1`**, and **`2`** respectively.

**`VatesLoadNode`**  

- **Required:** `dct_path` (**`STRING`**), either absolute or relative to ComfyUI’s **output** folder.  
- **Outputs:** `RETURN_TYPES` are **`IMAGE`** and **`STRING`**; `RETURN_NAMES` are **`image`** and **`workflow_json`**.

---

<h3 id="en-7-build--deployment">7. Build and deployment</h3>

**Toolchain expectations.** Install **Rust 1.70 or newer**, **Python 3.9 or newer**, and **Cargo**. ComfyUI needs **PyTorch**, **NumPy**, and **`folder_paths`** when you run the bundled nodes.

**ComfyUI / one-shot native module (recommended).** From the **`dct-core`** root (same folder as **`Cargo.toml`**, **`install.py`**, **`vates_nodes.py`**):

```bash
cd dct-core
python install.py
```

This runs **`cargo build --release --no-default-features --features python`**, then **aligns** artifacts next to **`vates_nodes.py`**: **Linux** copies **`target/release/libvates_core.so`** → **`./vates_core.so`** and applies **`chmod +x`**; **macOS** prefers **`target/release/libvates_core.dylib`** → **`./vates_core.so`** (falls back to **`.so`** names if present); **Windows** picks **`vates_core.cp*.pyd`** or **`vates_core.dll`** → **`./vates_core.pyd`**. It verifies **`import vates_core`** and may use a local wheel in **`wheels/`** or **`maturin develop`** as fallback. **Important:** **`cargo build --release --no-default-features`** **without** **`--features python`** does **not** build PyO3 and cannot satisfy **`import vates_core`**.

Restart ComfyUI after success. If the extension is missing, plugin **`__init__.py`** prints a short console hint to run **`python install.py`** in this repo.

**Sanity check:**

```bash
python check_vates.py
```

Expect **`Vates Core Bridge: SUCCESS`**.

**`Cargo.toml` `[lib]`:** **`cdylib`** is required for the Python extension. This repo also lists **`rlib`** so the in-tree **`vates`** binary and **`[[bench]]`** can **`use vates_core::...`**; **`cdylib` only** would break those targets.

**Command-line binary only (no Python module):**

```bash
cd dct-core
cargo build --release --no-default-features
```

**Python extension without `install.py` (alternative):**

```bash
maturin develop --release
```

**Manual copy on Linux / macOS** (if you built with **`--features python`** yourself): **`libvates_core.so`** or **`libvates_core.dylib`** → **`vates_core.so`** in the repo root.

```bash
# Linux
cp target/release/libvates_core.so ./vates_core.so
# macOS (rustc cdylib)
cp target/release/libvates_core.dylib ./vates_core.so
```

Add **`custom_nodes/ComfyUI-Vates`** or the whole **`dct-core`** tree under ComfyUI **custom_nodes**.

---

<h3 id="en-8-cli-vates">8. Command-line tool (`vates`)</h3>

Run these from the **`dct-core`** tree after a successful build:

```bash
cargo run --release --no-default-features --bin vates -- inspect path/to/file.dct
cargo run --release --no-default-features --bin vates -- inspect --json path/to/file.dct
cargo run --release --no-default-features --bin vates -- verify path/to/file.dct
```

- **`inspect`** prints a human-readable header summary.  
- **`inspect --json`** prints embedded workflow JSON to **stdout** when present.  
- **`verify`** performs full structural and checksum validation.

---

<h3 id="en-9-sdk-examples">9. SDK examples</h3>

**Python**

```python
import numpy as np
import vates_core as vc

chw = np.ascontiguousarray(np.random.rand(3, 64, 64).astype(np.float32))
vc.encode_tensor(chw, "out.dct", mode=0, fps=24.0, batch=1, metadata=None)
arr, wf = vc.decode_tensor_with_workflow("out.dct")

bhwc = np.ascontiguousarray(np.random.rand(10, 64, 64, 3).astype(np.float32))
vc.encode_batch(bhwc, "video.dct", fps=24.0, force_p2=False, header_mode=1, metadata=None)
```

`peek_dct_header(path)` returns **`(batch, channels, height, width, mode, reserved, fps)`**, in the same order as the on-disk header fields.

**Rust** — add the crate without default features if you only need the codec:

```toml
[dependencies]
vates_core = { path = "../dct-core", default-features = false }
```

```rust
use vates_core::Decoder;

let decoded = Decoder::decode_file_full("file.dct")?;
let _report = Decoder::verify_file("file.dct")?;
```

---

<h3 id="en-10-size-benchmark-illustrative">10. Size versus PNG (illustrative only)</h3>

The numbers below are **not** product guarantees. They are **rough order-of-magnitude examples** for **1024×1024 RGB** content across **64** frames, on typical desktop hardware. Always benchmark with **your own** frames and PNG settings.

| Metric | 64-file PNG sequence (example range) | One multi-frame `.dct` (dictionary + XXH3, example range) |
|--------|--------------------------------------|-----------------------------------------------------------|
| Total size on disk | About **40–120 MB** (highly content-dependent) | About **8–35 MB** |
| Uncompressed FP32 reference | — | About **768 MB** |
| Encode throughput | Often slower (per-file PNG encoding) | Often faster (parallel quantization and threaded Zstd) |

---

### Project layout

```
dct-core/
  src/core.rs              # format, encode/decode, dictionary, XXH3
  src/python_binding.rs
  src/main.rs               # vates CLI
  vates_nodes.py            # ComfyUI node definitions
  vates_server_hooks.py     # POST /vates/extract_workflow
  web/vates_dct_drop.js     # browser drag-and-drop helper
  custom_nodes/ComfyUI-Vates/
  install.py
  check_vates.py
```

**License:** MIT OR Apache-2.0
</details>

---

<a id="lang-zh"></a>

<details>
<summary><b>📘 简体中文文档（点击标题展开）</b></summary>

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

**环境：** Rust **1.70+**，Python **3.9+**，`cargo`；ComfyUI 需 **torch**、**numpy**、**folder_paths**。

**ComfyUI / 一键原生扩展（推荐）：** 在 **`dct-core`** 根目录（与 **`Cargo.toml`、`install.py`、`vates_nodes.py`** 同级）执行一次：

```bash
cd dct-core
python install.py
```

脚本会执行 **`cargo build --release --no-default-features --features python`**，再将产物**对齐**到仓库根：**Linux** 将 **`target/release/libvates_core.so`** 复制为 **`./vates_core.so`** 并 **`chmod +x`**；**macOS** 优先 **`target/release/libvates_core.dylib`** → **`./vates_core.so`**（若无则回退 **`.so`** 命名）；**Windows** 优先 **`vates_core.cp*.pyd`**，否则 **`vates_core.dll`**，统一对齐为 **`./vates_core.pyd`**。随后校验 **`import vates_core`**；亦可自动尝试 **`wheels/`** 本地 wheel 或 **`maturin develop`**。**注意：仅 **`cargo build --release --no-default-features`**、不加 **`--features python`** 时**不会**编译 PyO3，无法 **`import vates_core`**。

成功后**重启 ComfyUI**。若缺少内核，插件 **`__init__.py`** 会在控制台提示在本仓库执行 **`python install.py`**。

**自检：**

```bash
python check_vates.py
```

正常应输出 **`Vates Core Bridge: SUCCESS`**。

**`Cargo.toml` `[lib]`：** 必须包含 **`cdylib`**（Python 扩展）。本仓库同时保留 **`rlib`**，供同仓 **`vates`** 与 **`[[bench]]`** 以 Rust 方式链接本库；若 **`crate-type`** 仅有 **`cdylib`**，这些目标会无法编译。

**仅 CLI（无 Python 模块）：**

```bash
cd dct-core
cargo build --release --no-default-features
```

**不用 `install.py` 的替代方案：**

```bash
maturin develop --release
```

**Linux / macOS 手动对齐：** 若自行用 **`--features python`** 编译：

```bash
# Linux
cp target/release/libvates_core.so ./vates_core.so
# macOS（cargo cdylib 常见为 .dylib）
cp target/release/libvates_core.dylib ./vates_core.so
```

将 **`custom_nodes/ComfyUI-Vates`** 或整包 **`dct-core`** 放入 ComfyUI **`custom_nodes`**。

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
