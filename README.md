# Vates: High-Performance Video Storage Engine

**Vates v0.1.0** — Rust-backed `.dct` storage for ComfyUI (8-bit quantization + Zstd).

## Nodes

| Node | Purpose |
|------|---------|
| **`VatesSaveNode`** | Batched **`IMAGE`** (`[B,H,W,C]`). Each frame becomes one **`.dct`** under ComfyUI **output**, via **`vates_core.encode_tensor`**. Filename pattern: `{prefix}_{timestamp}_{index}.dct`. |
| **`VatesLoadNode`** | Loads a single **`.dct`** (absolute or relative to output) via **`vates_core.decode_tensor`**, returns **`IMAGE`** with shape **`[1,H,W,C]`**. |

Requirements: **`vates_core`** (this repo’s PyO3 module), **`torch`**, **`numpy`**, plus ComfyUI’s runtime for **`folder_paths`** when using the bundled nodes.

## Install

Clone the repo, then expose the plugin subtree to ComfyUI:

```bash
git clone https://github.com/sy00186/Comfyui-Output-node.git
# Copy or symlink custom_nodes/ComfyUI-Vates into your ComfyUI custom_nodes/
```

Install **`vates_core`** into the **same Python** ComfyUI uses:

- From source: **`maturin develop --release`** (repo root; needs Rust + maturin).
- Or drop a matching **`vates_core-*.whl`** into **`custom_nodes/ComfyUI-Vates/wheels/`** and run **`python install.py`** at the repo root.

Restart ComfyUI after installing the extension.

## Layout

```
./src                  # Rust core + PyO3 (module name: vates_core)
./custom_nodes/ComfyUI-Vates/nodes.py
./install.py           # Detects wheels / fallback to maturin
./check_vates.py       # Local smoke test helper
```

## License

MIT OR Apache-2.0
