# Vates（VATeS）— 高性能 `.dct` 张量资产格式 / High-Performance `.dct` Tensor Asset Format

**版本 Version:** 0.1.1（与 `Cargo.toml` 一致）

Vates 是面向 ComfyUI 与通用推理流水线的 **量化 + Zstandard 压缩** 存储方案：在 **32 字节二进制头**后接压缩载荷，可选 **嵌入 ComfyUI 工作流 JSON**，并支持 **视频批的多帧字典压缩** 与 **块级 XXH3 完整性校验**。

---

## 目录 Table of contents

1. [二进制协议：32 字节头 / Binary header (32 bytes)](#1-二进制协议32-字节头--binary-header-32-bytes)
2. [性能：零拷贝、并行量化、多线程 Zstd / Performance](#2-性能零拷贝并行量化多线程-zstd--performance)
3. [视频字典压缩 / Dictionary compression](#3-视频字典压缩--dictionary-compression-for-multi-frame)
4. [可靠性与 XXH3 / Integrity (XXH3)](#4-可靠性与-xxh3--integrity-xxh3)
5. [工作流资产化：嵌入与拖拽还原 / Workflow embedding & drag-restore](#5-工作流资产化嵌入与拖拽还原--workflow-embedding--drag-restore)
6. [ComfyUI 节点说明 / ComfyUI nodes](#6-comfyui-节点说明--comfyui-nodes)
7. [部署与编译 / Build & deployment](#7-部署与编译--build--deployment)
8. [CLI：`vates` 工具 / CLI tooling](#8-clivates-工具--cli-tooling)
9. [开发者 SDK 示例 / SDK examples](#9-开发者-sdk-示例--sdk-examples)
10. [体积与速度：与 PNG 序列对照（示例）/ Size vs PNG (illustrative)](#10-体积与速度示例对照表--illustrative-benchmark)

---

## 1. 二进制协议：32 字节头 / Binary header (32 bytes)

**English:** All valid `.dct` files start with a **32-byte big-endian header**. Byte layout is defined by `DctHeader::to_bytes_be` / `from_bytes_be` in `src/core.rs` (`HEADER_LEN = 32`).

| 偏移 Offset | 大小 Size | 字段 Field | 说明 Notes |
|-------------|-----------|------------|------------|
| 0–3 | 4 | **Magic** | 固定 ASCII **`VATS`**（`0x56 0x41 0x54 0x53`）。 |
| 4–5 | 2 | **Version** | `u16` BE，当前 **`FORMAT_VERSION = 1`**。非 1 则解码拒绝。 |
| 6 | 1 | **Mode** | `u8`，语义由 ComfyUI 节点约定：`0` = 图像序列 / `1` = 视频批 / `2` = 流式追加（见下文节点表）。 |
| 7 | 1 | **Reserved** | `u8`。**低 7 位** = **容器类型**；**最高位 `0x80`** = 头后是否存在 **`META` 工作流块**。 |
| 8–11 | 4 | **Batch (B)** | `u32` BE，帧数维度（单帧为 1）。 |
| 12–15 | 4 | **Channels (C)** | `u32` BE，如 RGB ⇒ 3。 |
| 16–19 | 4 | **Height (H)** | `u32` BE。 |
| 20–23 | 4 | **Width (W)** | `u32` BE。 |
| 24–27 | 4 | **FPS** | **IEEE-754 `f32` big-endian**（与常见本机 LE 不同，按头定义存储）。 |
| 28–31 | 4 | *Padding* | 当前实现中为 **全 0**（保留）。 |

### 1.1 `Reserved` 字节详解 / `Reserved` byte

**Chinese:**  
- **低 7 位（`reserved & 0x7F`）— 容器类型**  
  - **`0`**：单段 Zstd 载荷（适合单帧或兼容旧式单文件布局）。  
  - **`1`**：多帧容器（多块 Zstd；**无**每块 XXH3，历史兼容）。  
  - **`2`**：多帧容器 + **每一压缩块末尾附加 `u64` XXH3-64**（当前多帧写入默认路径）。  
- **最高位 `0x80`**：为 1 时，在 **32 字节头之后、主载荷之前** 增加可选元数据块（见下）。

**English:**  
Lower 7 bits select the **container layout**; bit **`0x80`** means an optional **workflow metadata block** immediately follows the header.

### 1.2 可选工作流块（`reserved & 0x80 != 0`）/ Optional workflow block

**Chinese:** 紧贴文件头之后出现：

1. **`META`**（4 字节 ASCII）  
2. **`u32` BE**：后续 **Zstd 压缩体**的字节长度  
3. **Zstd 压缩的 UTF-8 JSON 文本**（明文 JSON 内含 ComfyUI `prompt` / `extra_pnginfo` 等，由 Python 侧序列化）

**不含嵌入**时：`0x80` 为 0，**不写入**上述块，文件从 32 字节头直接进入各容器类型的主载荷，与旧版无元数据布局一致。

**English:** Layout: `META` + big-endian `u32` length + **zstd(compressed UTF-8 JSON)**. If the flag is clear, none of this is present.

### 1.3 主载荷（概要）/ Payload (overview)

**Chinese:**  
- **单帧容器（低 7 位 = 0）**：头（及可选 `META` 块）后接 **一段 Zstd**，解压后为：`min f32 LE` + `max f32 LE` + **逐通道 `u8` 量化**（CHW 顺序）。  
- **多帧容器（低 7 位 = 1 或 2）**：`u32` 字典长度 + 字典字节 + `u32` **块数量** + 每帧：`u32` 块长 + Zstd 帧 + （若 `=2`）`u64` XXH3。

**English:** Single-frame: one zstd blob per file after optional metadata. Multi-frame: **trained dictionary** (see below) + per-frame compressed blocks + optional **XXH3** trailers.

---

## 2. 性能：零拷贝、并行量化、多线程 Zstd / Performance

**Chinese — 零拷贝（Python ↔ Rust）**  
`vates_core.encode_tensor` 接受 **C 连续、`float32` 的 NumPy 三维数组（CHW）**；PyO3 + `PyReadonlyArray3` 在布局合法时直接 **借用** 底层缓冲区，Rust 侧用 `ArrayView1::from(slice)` 映射为连续 `f32` 切片，避免进入编码路径前对整帧再做一份 CPU 内存拷贝。若布局非标准，绑定会报错并提示先 `numpy.ascontiguousarray(..., dtype=float32)`。

**English — Zero-copy path**  
The Python API requires **standard-layout, contiguous `float32`** CHW arrays so Rust can borrow the buffer and wrap it as `ndarray::ArrayView1` for encoding.

**Chinese — 并行量化**  
全局 min/max 与逐元素 8-bit 量化使用 **Rayon**（`par_bridge` / `par_iter_mut`），在多核 CPU 上并行扫描与写回量化结果。

**English — Parallel quantization**  
`min_max_par` and `quantize_par` use Rayon for parallel reduction and per-pixel quantization.

**Chinese — 多线程 Zstd**  
`zstd` 依赖启用 **`zstdmt`**。编码封装 `zstd_encode_mt` 会根据 `available_parallelism()` 设置编码器多线程 worker（**上限 8**），在压缩大块明文时提高吞吐。

**English — Multi-threaded Zstd**  
Encoder uses `Encoder::multithread` with up to **8** workers when available.

---

## 3. 视频字典压缩 / Dictionary compression (multi-frame)

**Chinese:** 对 **多帧 BHWC** 写入时，实现会从最多前 **32** 帧（`P2_MAX_TRAIN_FRAMES`）采样，将每帧量化后的明文拼接训练 **Zstd 字典**（长度受 **`P2_DICT_MAX_BYTES`（64 KiB）** 等约束）。后续各帧优先使用 **预计算字典** 进行 Zstd 压缩，使相邻帧共享统计规律，通常比「逐帧独立 PNG」体积更小、且为 **单文件** 归档。

**English:** Multi-frame encodes build a **shared Zstd dictionary** from up to 32 training frames (quantized payload), capped by **64 KiB**, then compress each frame with `EncoderDictionary` when beneficial.

---

## 4. 可靠性与 XXH3 / Integrity (XXH3)

**Chinese:** 当 **`reserved` 低 7 位 = `2`** 时，每个压缩帧数据块之后写入 **`u64` big-endian** 的 **XXH3-64** 摘要（对 **压缩字节**本身计算）。解码或校验时若不一致，返回 **`DctError::DataCorruption`**（Python 侧多为 `ValueError`，消息含 `VATES_CORRUPTION`）。用于检测存储损坏、截断或恶意篡改。

**English:** Container **`2`** appends **XXH3-64** (`u64` BE) per compressed block; mismatch yields **`DataCorruption`** with block index and expected/got hashes.

---

## 5. 工作流资产化：嵌入与拖拽还原 / Workflow embedding & drag-restore

**Chinese — 嵌入**  
`VatesSaveNode.save` 通过 ComfyUI **`hidden`** 输入读取 **`prompt`** 与 **`extra_pnginfo`**，序列化为 JSON 字符串，经 `metadata` 参数传入 Rust，写入 **`META` + Zstd(JSON)**。单帧、视频批、流式写入路径均支持（流式追加不修改已写入的元数据策略以当前实现为准）。

**English — Embedding**  
Save path packs `prompt` / `extra_pnginfo` into JSON and passes it as **`metadata`** to the Rust encoder when non-empty.

**Chinese — 读取与节点输出**  
`VatesLoadNode` 输出 **`IMAGE` + `STRING`（`workflow_json`）**，内部调用 **`decode_tensor_with_workflow`**。未嵌入时第二路为空字符串。

**English — Load node**  
Returns **`decode_tensor_with_workflow`**: raster + optional embedded JSON string.

**Chinese — 拖拽还原**  
浏览器扩展 **`web/vates_dct_drop.js`** 在窗口 **`drop`** `.dct` 时，向 **`POST /vates/extract_workflow`**（`multipart` 字段名 **`file`**）上传文件；服务端用 **`read_embedded_workflow_json`** 解压元数据并返回 JSON；前端解析其中的 **`extra_pnginfo.workflow`** 或顶层 **`workflow`** 并调用 **`app.loadGraphData`**（若仅有 API `prompt` 则尝试 `loadApiFormat` / `importApiJson`，视 ComfyUI 版本而定）。  
路由注册见 **`vates_server_hooks.py`**；插件 **`WEB_DIRECTORY`** 需指向含该 JS 的 `web/` 目录。

**English — Drag-and-drop**  
Custom route **`POST /vates/extract_workflow`** + extension **`vates_dct_drop.js`** reloads the graph from embedded workflow JSON when a `.dct` is dropped on the page.

---

## 6. ComfyUI 节点说明 / ComfyUI nodes

实现文件：**`vates_nodes.py`**（类名与 `NODE_CLASS_MAPPINGS` 一致）。

### 6.1 `VatesSaveNode`（`OUTPUT_NODE = True`）

| 项目 Item | 内容 Value |
|-----------|------------|
| **必填输入 Required** | `images` (**`IMAGE`**，四维 **`[B,H,W,C]`** BHWC）、`filename_prefix` (**`STRING`**)、`save_mode`（枚举）、`fps` (**`FLOAT`** 0.1–240)、`stream_id` (**`STRING`**) |
| **`save_mode` 枚举** | **`Image (Sequence)`** / **`Video (Batch)`** / **`Streaming (Append)`** |
| **隐藏输入 Hidden** | **`prompt`** = `PROMPT`，**`extra_pnginfo`** = `EXTRA_PNGINFO` |
| **与头 `mode` 对应** | `0` = Image (Sequence)；`1` = Video (Batch)；`2` = Streaming (Append) |
| **输出** | 无元组输出；通过 UI 文本提示保存路径（后台异步任务时用 ComfyUI 日志辅助观察） |

**English:** Hidden inputs receive the live **prompt** and **extra_pnginfo** from ComfyUI for JSON embedding.

### 6.2 `VatesLoadNode`

| 项目 Item | 内容 Value |
|-----------|------------|
| **必填输入** | **`dct_path`** (**`STRING`**)：绝对路径或相对 **ComfyUI `output`** |
| **`RETURN_TYPES`** | **`IMAGE`**, **`STRING`** |
| **`RETURN_NAMES`** | **`image`**, **`workflow_json`** |

**English:** Second output is the embedded workflow JSON or empty string.

---

## 7. 部署与编译 / Build & deployment

### 7.1 环境依赖 / Prerequisites

| 依赖 Dependency | 建议版本 Suggested |
|-----------------|-------------------|
| **Rust** | **1.70+**（项目 `edition = "2021"`） |
| **Python** | **3.9+**（与 ComfyUI、PyO3 0.22 对齐） |
| **构建 Build** | **`cargo`**；Python 扩展推荐 **`maturin`** |
| **ComfyUI 侧** | 与节点一致的 **`torch`**, **`numpy`**；运行时需要能 `import folder_paths` |

### 7.2 纯 Rust CLI 与静态核心库（关闭 Python 特性）/ CLI-only build

**中文：** 仅构建**命令行 `vates` 二进制**与可链接的 **Rust `rlib/cdylib`（无 PyO3 符号）** 时：

```bash
cd dct-core
cargo build --release --no-default-features
```

产物示例：`target/release/vates(.exe)`；库便于 `default-features = false` 依赖。

**English:** Disables **`python`** feature (no `#[pymodule]`). Use when you only need the **`vates` CLI** or Rust **`Decoder`/`Encoder`** without the extension module.

### 7.3 含 Python 扩展 `vates_core` / Python extension

**中文（推荐）：**

```bash
cd dct-core
maturin develop --release
# 或: maturin build --release
```

**English:** Default crate features include **`python`** → builds the **`vates_core`** shared module for the active interpreter.

### 7.4 Linux / DataStone：`.so` 物理校准 / Shared library naming (Linux)

**中文：** 某些环境从 `target/release` 产出名为 **`libvates_core.so`** 的 `cdylib`，而嵌入运行时可能要求可导入名为 **`vates_core.so`** 的文件。可在发布目录执行（路径按实际调整）：

```bash
cp target/release/libvates_core.so ./vates_core.so
# 或使用符号链接:
# ln -sf target/release/libvates_core.so vates_core.so
```

请最终以 **你的 Python 与部署规范** 为准（亦常见 `vates_core.cpython-XY-x86_64-linux-gnu.so` 等由 **maturin** 生成的文件名）。

**English:** Some hosts expect **`vates_core.so`** next to plugins; copy or symlink from **`libvates_core.so`** if your pipeline emits the latter.

### 7.5 安装 ComfyUI 插件 / Install as custom node

**中文：** 将 **`custom_nodes/ComfyUI-Vates/`** 链入 ComfyUI，或使用仓库根 **`__init__.py`** 将整个 **`dct-core`** 作为自定义节点包；确保 **`vates_core`** 已装入 **同一 Python 环境**。可使用根目录 **`install.py`** 或预置 **`wheels/`** 下的 wheel。

**English:** Symlink or copy **`ComfyUI-Vates`**; install **`vates_core`** into the same venv ComfyUI uses.

---

## 8. CLI：`vates` 工具 / CLI tooling

**中文：** 二进制名 **`vates`**（`src/main.rs`）。常用：

```bash
# 人类可读：打印头字段、容器类型、是否含嵌入工作流等
cargo run --release --no-default-features --bin vates -- inspect path/to/file.dct

# 仅 stdout 输出嵌入的原始 UTF-8 JSON 文本（若无则打印说明到 stderr）
cargo run --release --no-default-features --bin vates -- inspect --json path/to/file.dct

# 校验（含多帧逐块 XXH3 等）
cargo run --release --no-default-features --bin vates -- verify path/to/file.dct
```

**English:** **`inspect`** shows the header table; **`inspect --json`** dumps embedded JSON; **`verify`** runs full validation.

---

## 9. 开发者 SDK 示例 / SDK examples

### 9.1 Python（`import vates_core`）

绑定签名以 **`src/python_binding.rs`** 为准；默认 **`metadata`** / **`metadata=None`**。

```python
import numpy as np
import vates_core as vc

# 单帧 CHW float32，C 连续
chw = np.ascontiguousarray(np.random.rand(3, 64, 64).astype(np.float32))
vc.encode_tensor(chw, "out.dct", mode=0, fps=24.0, batch=1, metadata=None)

# 解码为 float32 数组（CHW 或 BCHW）；含工作流时：
arr, wf = vc.decode_tensor_with_workflow("out.dct")
# 仅读嵌入 JSON（无解压整帧）：
meta = vc.read_embedded_workflow_json("out.dct")  # str | None

# 多帧 BHWC（示例）
bhwc = np.ascontiguousarray(np.random.rand(10, 64, 64, 3).astype(np.float32))
vc.encode_batch(
    bhwc,
    "video.dct",
    fps=24.0,
    force_p2=False,
    header_mode=1,  # 与文件头 DctHeader.mode 一致；Video=1
    metadata='{"prompt":{}}',
)
```

**窥视文件头：** `peek_dct_header(path) → (batch, channels, height, width, mode, reserved, fps)`（与 Rust 头字段一致）。

### 9.2 Rust（`cargo add` / path 依赖）

**`Cargo.toml` 示例：**

```toml
[dependencies]
vates_core = { path = "../dct-core", default-features = false }
```

**代码示例：**

```rust
use vates_core::Decoder;
use std::path::Path;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let p = Path::new("file.dct");
    let decoded = Decoder::decode_file_full(p)?;
    let _hdr = decoded.header;
    let _floats = decoded.floats;
    let _maybe_json = decoded.workflow_json;

    if let Some(s) = Decoder::read_embedded_workflow_json(p)? {
        println!("embedded JSON length: {}", s.len());
    }
    let report = Decoder::verify_file(p)?;
    println!("{}", report);
    Ok(())
}
```

---

## 10. 体积与速度：示例对照表 / Illustrative benchmark

**重要说明 / Disclaimer**  
下列数字 **不是** 性能基准承诺，仅基于典型桌面硬件与「照片类 / 中等细节」内容的 **数量级示意**，便于对比格式定位。真实结果强依赖 **CPU、PNG 工具参数、画面内容、是否 alpha、磁盘与 I/O**。请在目标环境用自有数据 **实测**。

**场景 Scenario：** 每帧 **1024×1024**，**RGB**，共 **64** 帧；Vates 使用 **8-bit 量化 + 多帧字典 + zstd**；PNG 序列为 **64 个独立 PNG**（无损或标准压缩设定）。

| 指标 Metric | PNG 序列（示例）PNG sequence (illustrative) | 单文件 `.dct`（P2 + 字典 + XXH3）Single `.dct` (illustrative) |
|-------------|-----------------------------------------------|---------------------------------------------------------------|
| **总输出体积 Total size** | 约 **40–120 MB**（内容敏感，范围大） | 约 **8–35 MB**（量化 + 字典通常明显小于逐帧无损 PNG） |
| **未压缩 FP32 理论体积 Raw FP32** | — | 约 **768 MB**（64×3×H×W×4 B） |
| **编码墙钟（相对）Encode wall-clock** | 逐文件 PNG 编码，常 **更慢** | **并行量化 + zstd 多线程**，批量常 **更快**（依核数变化） |

**English:** Use this table for **order-of-magnitude planning** only; run your own benchmarks for SLAs.

---

## 仓库布局 / Repository layout（概要）

```
dct-core/
  src/core.rs          # 协议、编解码、字典、XXH3
  src/python_binding.rs
  src/main.rs           # vates CLI
  vates_nodes.py        # ComfyUI 节点
  vates_server_hooks.py # POST /vates/extract_workflow
  web/vates_dct_drop.js # 拖拽还原前端
  custom_nodes/ComfyUI-Vates/
  install.py / check_vates.py
```

---

## 许可证 / License

**MIT OR Apache-2.0**（与 `Cargo.toml` 声明一致）。
