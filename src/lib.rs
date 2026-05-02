//! Python 扩展入口：`vates_core`（Maturin / PyO3 加载）。
//!
//! 核心算法见 [`mod@core`]。

mod core;

pub use core::{
    Decoder, DctError, DctHeader, Encoder, FORMAT_VERSION, HEADER_LEN, MAGIC, synthetic_chw,
};

use ndarray::ArrayView1;
use numpy::{PyArray, PyReadonlyArrayDyn};
use numpy::PyUntypedArrayMethods;
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyModule, PyModuleMethods};

/// 将底层 `DctError` 映射为 Python 侧惯例异常类型。
fn dct_error_to_py(e: DctError) -> PyErr {
    match e {
        DctError::Io(_) | DctError::Zstd(_) => PyIOError::new_err(e.to_string()),
        _ => PyValueError::new_err(e.to_string()),
    }
}

fn u32_fit(dim: usize, axis: &str) -> PyResult<u32> {
    u32::try_from(dim).map_err(|_| {
        PyValueError::new_err(format!(
            "轴 `{}` 的尺寸 {} 超出 u32 可表示范围，当前格式不支持",
            axis, dim
        ))
    })
}

fn channels_as_u8(c: usize) -> PyResult<u8> {
    u8::try_from(c).map_err(|_| {
        PyValueError::new_err(format!(
            "通道数 {} 超过单字节上限 255；请拆分批次或使用更少通道",
            c
        ))
    })
}

/// 编码三维 `float32` 张量为 `.dct` 文件。
///
/// Python 侧传入 `numpy.ndarray`（`dtype=float32`）。Rust 参数类型使用 [`PyReadonlyArrayDyn`]，
/// 由 PyO3 从任意数组对象提取，等价于对你文档中的 `PyArrayDyn<f32>` 做只读、零额外拷贝的借用。
///
/// **内存：** `as_slice()` 在 C 连续时直接映射到底层缓冲区；不在 Rust/Python 边界复制整块 `f32`。
///
/// **GIL：** 量化、Zstd、写盘在 [`Python::allow_threads`] 内执行。
///
/// **安全约定：** 函数返回前 Python 不得释放/替换该数组缓冲区，也不得在未同步下并发写入同一可写缓冲区。
///
/// **形状：** `ndim == 3`，解释为 CHW：`(channels, height, width)`。
#[pyfunction]
#[pyo3(signature = (tensor, output_path))]
pub fn encode_tensor<'py>(
    py: Python<'py>,
    tensor: PyReadonlyArrayDyn<'py, f32>,
    output_path: &str,
) -> PyResult<()> {
    if !tensor.is_c_contiguous() {
        return Err(PyValueError::new_err(
            "tensor 必须为 C 连续（flags['C_CONTIGUOUS']），否则无法零拷贝；可先 numpy.ascontiguousarray(tensor, dtype=numpy.float32)。",
        ));
    }

    let shape = tensor.shape();
    if shape.len() != 3 {
        return Err(PyValueError::new_err(format!(
            "期望 ndim=3 的 CHW 张量，当前 ndim={}",
            shape.len()
        )));
    }

    let c_dim = shape[0];
    let h_dim = shape[1];
    let w_dim = shape[2];

    let channels_u8 = channels_as_u8(c_dim)?;
    let height = u32_fit(h_dim, "height (H)")?;
    let width = u32_fit(w_dim, "width (W)")?;

    let len = tensor.len();
    let expected = c_dim
        .checked_mul(h_dim)
        .and_then(|x| x.checked_mul(w_dim))
        .ok_or_else(|| PyValueError::new_err("形状乘积溢出 usize"))?;
    if len != expected {
        return Err(PyValueError::new_err(format!(
            "底层缓冲区元素数 {} 与形状 {:?} 推导的元素数 {} 不一致",
            len, shape, expected
        )));
    }

    let slice = tensor.as_slice().map_err(|_| {
        PyValueError::new_err("内部错误：已判定 C 连续但无法取得 slice，请回报 numpy / pyo3 版本")
    })?;

    let path = output_path.to_string();
    const ZSTD_LEVEL: i32 = 3;

    // `slice` 生命周期绑定到 `tensor`，本函数在 `allow_threads` 返回后才退出，`tensor` 始终有效。
    py.allow_threads(|| {
        let view = ArrayView1::from(slice);
        Encoder::encode_file(
            view,
            width,
            height,
            channels_u8,
            0,
            path.as_str(),
            ZSTD_LEVEL,
        )
    })
    .map_err(dct_error_to_py)?;

    Ok(())
}

/// 从 `.dct` 解码为 CHW、`float32`、`ndim=3` 的 NumPy 数组。
///
/// **GIL：** 读盘、解压、反量化在 `allow_threads` 内完成。
///
/// **分配：** 解压结果为新的 `Vec<f32>`，再在 GIL 下构造输出 ndarray（输出侧必然有新分配）。
#[pyfunction]
#[pyo3(signature = (input_path))]
pub fn decode_tensor<'py>(
    py: Python<'py>,
    input_path: &str,
) -> PyResult<Py<PyArray<f32, ndarray::Dim<ndarray::IxDynImpl>>>> {
    let path = input_path.to_string();

    let decoded = py
        .allow_threads(move || Decoder::decode_file(path))
        .map_err(dct_error_to_py)?;

    let (hdr, floats) = decoded;

    let shape = ndarray::IxDyn(&[
        hdr.channels as usize,
        hdr.height as usize,
        hdr.width as usize,
    ]);
    let arr = ndarray::Array::from_shape_vec(shape, floats).map_err(|e| {
        PyValueError::new_err(format!("解码数据与文件头维度不一致，无法重塑为 CHW：{}", e))
    })?;

    let bound = PyArray::<f32, ndarray::IxDyn>::from_owned_array_bound(py, arr);
    Ok(bound.unbind())
}

#[pymodule]
fn vates_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(encode_tensor, m)?)?;
    m.add_function(wrap_pyfunction!(decode_tensor, m)?)?;
    Ok(())
}
