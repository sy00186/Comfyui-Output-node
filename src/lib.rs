//! Vates：`.dct` 量化 + zstd 核心库；可选特性 **`python`** 提供 PyO3 模块 `vates_core`。
//!
//! 纯 Rust 依赖请使用：`vates_core = { version = "...", default-features = false }`。

mod core;

pub use core::{
    container_kind_from_reserved, reserved_with_workflow_json_flag, CONTAINER_LEGACY,
    CONTAINER_P2_DICT_BLOCKS, CONTAINER_P2_DICT_BLOCKS_XXH3, DecodedDct, Decoder, DctError,
    DctHeader, Encoder, FORMAT_VERSION, HEADER_LEN, MAGIC, RESERVED_FLAG_LBM_EMBEDDED_WORKFLOW_JSON,
    WORKFLOW_META_MAGIC, synthetic_chw,
};

#[cfg(feature = "python")]
mod python_binding;

#[cfg(feature = "python")]
use pyo3::prelude::*;
#[cfg(feature = "python")]
use pyo3::types::PyModule;

#[cfg(feature = "python")]
#[pymodule]
fn vates_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    python_binding::register(m)
}
