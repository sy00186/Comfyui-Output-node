//! PyO3 绑定（`--features python`，默认开启）。

use crate::{Decoder, DctError, Encoder};
use ndarray::ArrayView1;
use numpy::{PyArray, PyReadonlyArray3, PyReadonlyArray4};
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAnyMethods, PyModule, PyModuleMethods};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_function(wrap_pyfunction!(encode_tensor, m)?)?;
    m.add_function(wrap_pyfunction!(encode_batch, m)?)?;
    m.add_function(wrap_pyfunction!(encode_batch_async, m)?)?;
    m.add_function(wrap_pyfunction!(append_to_vats, m)?)?;
    m.add_function(wrap_pyfunction!(append_to_vats_async, m)?)?;
    m.add_function(wrap_pyfunction!(peek_dct_header, m)?)?;
    m.add_function(wrap_pyfunction!(get_pending_tasks, m)?)?;
    m.add_function(wrap_pyfunction!(await_pending_writes, m)?)?;
    m.add_function(wrap_pyfunction!(decode_tensor, m)?)?;
    m.add_function(wrap_pyfunction!(decode_tensor_with_workflow, m)?)?;
    m.add_function(wrap_pyfunction!(read_embedded_workflow_json, m)?)?;
    Ok(())
}

fn dct_error_to_py(e: DctError) -> PyErr {
    match e {
        DctError::Io(_) | DctError::Zstd(_) => PyIOError::new_err(e.to_string()),
        DctError::DataCorruption { .. } => PyValueError::new_err(e.to_string()),
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

fn encode_timing_enabled() -> bool {
    matches!(
        std::env::var("VATES_ENCODE_TIMING").as_deref(),
        Ok("1") | Ok("true") | Ok("yes")
    )
}

/// 后台写入队列中的任务数（已派发、尚未执行完 `fetch_sub` 的线程任务）。
static PENDING_TASKS: AtomicUsize = AtomicUsize::new(0);

fn try_python_log_error(line: &str) {
    let _ = Python::with_gil(|py| -> PyResult<()> {
        let logging = py.import_bound("logging")?;
        let logger = logging.getattr("getLogger")?.call1(("vates_core",))?;
        logger.call_method1("error", (line,))?;
        Ok(())
    });
}

fn spawn_save_task(
    name: &'static str,
    path_display: String,
    f: impl FnOnce() -> Result<(), DctError> + Send + 'static,
) {
    PENDING_TASKS.fetch_add(1, Ordering::SeqCst);
    std::thread::spawn(move || {
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
        match r {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                let line = format!("[vates_core] {name} 写入失败 ({path_display}): {e}");
                eprintln!("{line}");
                try_python_log_error(&line);
            }
            Err(p) => {
                let line = format!("[vates_core] {name} 线程 panic ({path_display}): {p:?}");
                eprintln!("{line}");
                try_python_log_error(&line);
            }
        }
        PENDING_TASKS.fetch_sub(1, Ordering::SeqCst);
    });
}

#[pyfunction]
#[pyo3(signature = (tensor, output_path, mode, fps, batch=1, metadata=None))]
fn encode_tensor<'py>(
    py: Python<'py>,
    tensor: PyReadonlyArray3<'py, f32>,
    output_path: &str,
    mode: u8,
    fps: f32,
    batch: u32,
    metadata: Option<String>,
) -> PyResult<()> {
    let arr = tensor.as_array();
    if !arr.is_standard_layout() {
        return Err(PyValueError::new_err(
            "tensor 须为 C 连续且标准布局，方可零拷贝；请先 numpy.ascontiguousarray(..., dtype=float32)。",
        ));
    }
    debug_assert!(arr.is_standard_layout());

    let slice = arr
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("tensor 无法展平为连续切片（布局异常）"))?;

    if batch < 1 {
        return Err(PyValueError::new_err("batch 须为 >= 1 的整数（写入头字段 B）"));
    }

    let (c_dim, h_dim, w_dim) = arr.dim();
    let channels_u32 = u32_fit(c_dim, "channels (C)")?;
    let height = u32_fit(h_dim, "height (H)")?;
    let width = u32_fit(w_dim, "width (W)")?;

    let len = slice.len();
    let expected_3d = c_dim
        .checked_mul(h_dim)
        .and_then(|x| x.checked_mul(w_dim))
        .ok_or_else(|| PyValueError::new_err("形状乘积溢出 usize"))?;
    if len != expected_3d {
        return Err(PyValueError::new_err(format!(
            "底层缓冲区元素数 {} 与形状 {:?} 推导的元素数 {} 不一致",
            len,
            (c_dim, h_dim, w_dim),
            expected_3d
        )));
    }

    let expected_batched = (batch as usize)
        .checked_mul(c_dim)
        .and_then(|x| x.checked_mul(h_dim))
        .and_then(|x| x.checked_mul(w_dim))
        .ok_or_else(|| PyValueError::new_err("B*C*H*W 溢出 usize"))?;
    if len != expected_batched {
        return Err(PyValueError::new_err(format!(
            "张量元素数 {} 与头字段 B*C*H*W (= {} * {} * {} * {} = {}) 不一致；\
             单帧编码请传 batch=1",
            len, batch, c_dim, h_dim, w_dim, expected_batched
        )));
    }

    let path = std::path::PathBuf::from(output_path);
    const ZSTD_LEVEL: i32 = 3;
    let time_it = encode_timing_enabled();
    let t0 = time_it.then(std::time::Instant::now);

    py.allow_threads(|| {
        let view = ArrayView1::from(slice);
        Encoder::encode_file(
            view,
            batch,
            channels_u32,
            height,
            width,
            mode,
            fps,
            &path,
            ZSTD_LEVEL,
            metadata.as_deref(),
        )
    })
    .map_err(dct_error_to_py)?;

    if let Some(t0) = t0 {
        eprintln!(
            "[vates_core] encode_tensor wall {} ns (path={})",
            t0.elapsed().as_nanos(),
            output_path
        );
    }

    Ok(())
}

#[pyfunction]
#[pyo3(signature = (tensor_bhwc, output_path, fps, force_p2=false, header_mode=1, metadata=None))]
fn encode_batch<'py>(
    py: Python<'py>,
    tensor_bhwc: PyReadonlyArray4<'py, f32>,
    output_path: &str,
    fps: f32,
    force_p2: bool,
    header_mode: u8,
    metadata: Option<String>,
) -> PyResult<()> {
    let arr = tensor_bhwc.as_array();
    if !arr.is_standard_layout() {
        return Err(PyValueError::new_err(
            "tensor_bhwc 须为 C 连续 BHWC；请先 numpy.ascontiguousarray(..., dtype=float32)。",
        ));
    }
    let slice = arr
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("BHWC 无法展平为连续切片"))?;

    let (b_dim, h_dim, w_dim, c_dim) = arr.dim();
    let batch = u32_fit(b_dim, "batch (B)")?;
    let height = u32_fit(h_dim, "height (H)")?;
    let width = u32_fit(w_dim, "width (W)")?;
    let channels = u32_fit(c_dim, "channels (C)")?;

    let path = std::path::PathBuf::from(output_path);
    const ZSTD_LEVEL: i32 = 3;
    let time_it = encode_timing_enabled();
    let t0 = time_it.then(std::time::Instant::now);

    py.allow_threads(|| {
        Encoder::encode_batch_bhwc_file(
            slice,
            batch,
            height,
            width,
            channels,
            fps,
            &path,
            ZSTD_LEVEL,
            force_p2,
            header_mode,
            metadata.as_deref(),
        )
    })
    .map_err(dct_error_to_py)?;

    if let Some(t0) = t0 {
        eprintln!(
            "[vates_core] encode_batch wall {} ns (path={}, B={})",
            t0.elapsed().as_nanos(),
            output_path,
            batch
        );
    }

    Ok(())
}

#[pyfunction]
#[pyo3(signature = (tensor_bhwc, output_path, fps, force_p2=false, header_mode=1, metadata=None))]
fn encode_batch_async<'py>(
    py: Python<'py>,
    tensor_bhwc: PyReadonlyArray4<'py, f32>,
    output_path: &str,
    fps: f32,
    force_p2: bool,
    header_mode: u8,
    metadata: Option<String>,
) -> PyResult<()> {
    let arr = tensor_bhwc.as_array();
    if !arr.is_standard_layout() {
        return Err(PyValueError::new_err(
            "tensor_bhwc 须为 C 连续 BHWC；请先 numpy.ascontiguousarray(..., dtype=float32)。",
        ));
    }
    let slice = arr
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("BHWC 无法展平为连续切片"))?;

    let (b_dim, h_dim, w_dim, c_dim) = arr.dim();
    let batch = u32_fit(b_dim, "batch (B)")?;
    let height = u32_fit(h_dim, "height (H)")?;
    let width = u32_fit(w_dim, "width (W)")?;
    let channels = u32_fit(c_dim, "channels (C)")?;

    let data: Vec<f32> = slice.to_vec();
    let path = PathBuf::from(output_path);
    let path_display = output_path.to_string();
    const ZSTD_LEVEL: i32 = 3;
    let meta = metadata.clone();

    py.allow_threads(|| {
        spawn_save_task(
            "encode_batch_async",
            path_display.clone(),
            move || {
                Encoder::encode_batch_bhwc_file(
                    &data,
                    batch,
                    height,
                    width,
                    channels,
                    fps,
                    &path,
                    ZSTD_LEVEL,
                    force_p2,
                    header_mode,
                    meta.as_deref(),
                )
            },
        );
    });

    Ok(())
}

#[pyfunction]
#[pyo3(signature = (tensor_bhwc, output_path, fps))]
fn append_to_vats<'py>(
    py: Python<'py>,
    tensor_bhwc: PyReadonlyArray4<'py, f32>,
    output_path: &str,
    fps: f32,
) -> PyResult<()> {
    let arr = tensor_bhwc.as_array();
    if !arr.is_standard_layout() {
        return Err(PyValueError::new_err(
            "tensor_bhwc 须为 C 连续 BHWC；请先 numpy.ascontiguousarray(..., dtype=float32)。",
        ));
    }
    let slice = arr
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("BHWC 无法展平为连续切片"))?;

    let (b_dim, h_dim, w_dim, c_dim) = arr.dim();
    let b_new = u32_fit(b_dim, "batch (B)")?;
    let height = u32_fit(h_dim, "height (H)")?;
    let width = u32_fit(w_dim, "width (W)")?;
    let channels = u32_fit(c_dim, "channels (C)")?;

    let path = std::path::PathBuf::from(output_path);
    const ZSTD_LEVEL: i32 = 3;

    py.allow_threads(|| {
        Encoder::append_p2_bhwc_frames(
            &path,
            slice,
            b_new,
            height,
            width,
            channels,
            fps,
            ZSTD_LEVEL,
        )
    })
    .map_err(dct_error_to_py)?;

    Ok(())
}

#[pyfunction]
#[pyo3(signature = (tensor_bhwc, output_path, fps))]
fn append_to_vats_async<'py>(
    py: Python<'py>,
    tensor_bhwc: PyReadonlyArray4<'py, f32>,
    output_path: &str,
    fps: f32,
) -> PyResult<()> {
    let arr = tensor_bhwc.as_array();
    if !arr.is_standard_layout() {
        return Err(PyValueError::new_err(
            "tensor_bhwc 须为 C 连续 BHWC；请先 numpy.ascontiguousarray(..., dtype=float32)。",
        ));
    }
    let slice = arr
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("BHWC 无法展平为连续切片"))?;

    let (b_dim, h_dim, w_dim, c_dim) = arr.dim();
    let b_new = u32_fit(b_dim, "batch (B)")?;
    let height = u32_fit(h_dim, "height (H)")?;
    let width = u32_fit(w_dim, "width (W)")?;
    let channels = u32_fit(c_dim, "channels (C)")?;

    let data: Vec<f32> = slice.to_vec();
    let path = PathBuf::from(output_path);
    let path_display = output_path.to_string();
    const ZSTD_LEVEL: i32 = 3;

    py.allow_threads(|| {
        spawn_save_task(
            "append_to_vats_async",
            path_display.clone(),
            move || {
                Encoder::append_p2_bhwc_frames(
                    &path,
                    &data,
                    b_new,
                    height,
                    width,
                    channels,
                    fps,
                    ZSTD_LEVEL,
                )
            },
        );
    });

    Ok(())
}

#[pyfunction]
fn peek_dct_header(path: &str) -> PyResult<(u32, u32, u32, u32, u8, u8, f32)> {
    let h = Decoder::read_header_only(path).map_err(dct_error_to_py)?;
    Ok((
        h.batch,
        h.channels,
        h.height,
        h.width,
        h.mode,
        h.reserved,
        h.fps,
    ))
}

#[pyfunction]
fn get_pending_tasks() -> usize {
    PENDING_TASKS.load(Ordering::SeqCst)
}

#[pyfunction]
fn await_pending_writes(py: Python<'_>) -> PyResult<()> {
    py.allow_threads(|| loop {
        if PENDING_TASKS.load(Ordering::SeqCst) == 0 {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(4));
    });
    Ok(())
}

#[pyfunction]
#[pyo3(signature = (input_path))]
fn decode_tensor<'py>(
    py: Python<'py>,
    input_path: &str,
) -> PyResult<Py<PyArray<f32, ndarray::Dim<ndarray::IxDynImpl>>>> {
    let path = input_path.to_string();

    let decoded = py
        .allow_threads(move || Decoder::decode_file_full(path))
        .map_err(dct_error_to_py)?;

    let hdr = decoded.header;
    let floats = decoded.floats;

    let shape = if hdr.batch <= 1 {
        ndarray::IxDyn(&[
            hdr.channels as usize,
            hdr.height as usize,
            hdr.width as usize,
        ])
    } else {
        ndarray::IxDyn(&[
            hdr.batch as usize,
            hdr.channels as usize,
            hdr.height as usize,
            hdr.width as usize,
        ])
    };
    let arr = ndarray::Array::from_shape_vec(shape, floats).map_err(|e| {
        PyValueError::new_err(format!("解码数据与文件头维度不一致：{}", e))
    })?;

    let bound = PyArray::<f32, ndarray::IxDyn>::from_owned_array_bound(py, arr);
    Ok(bound.unbind())
}

#[pyfunction]
#[pyo3(signature = (input_path))]
fn decode_tensor_with_workflow<'py>(
    py: Python<'py>,
    input_path: &str,
) -> PyResult<(
    Py<PyArray<f32, ndarray::Dim<ndarray::IxDynImpl>>>,
    Option<String>,
)> {
    let path = input_path.to_string();
    let d = py
        .allow_threads(move || Decoder::decode_file_full(path))
        .map_err(dct_error_to_py)?;
    let hdr = d.header;
    let floats = d.floats;
    let wf = d.workflow_json;
    let shape = if hdr.batch <= 1 {
        ndarray::IxDyn(&[
            hdr.channels as usize,
            hdr.height as usize,
            hdr.width as usize,
        ])
    } else {
        ndarray::IxDyn(&[
            hdr.batch as usize,
            hdr.channels as usize,
            hdr.height as usize,
            hdr.width as usize,
        ])
    };
    let arr = ndarray::Array::from_shape_vec(shape, floats).map_err(|e| {
        PyValueError::new_err(format!("解码数据与文件头维度不一致：{}", e))
    })?;
    let bound = PyArray::<f32, ndarray::IxDyn>::from_owned_array_bound(py, arr);
    Ok((bound.unbind(), wf))
}

#[pyfunction]
fn read_embedded_workflow_json(path: &str) -> PyResult<Option<String>> {
    Decoder::read_embedded_workflow_json(path).map_err(dct_error_to_py)
}
