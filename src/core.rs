//! Vates `.dct`：**32 字节大端文件头**（`VATS`）+ 载荷。
//!
//! **P0/P1 单帧（`reserved=0`）：** `[Header] + [单段 zstd(min,max,u8×CHW)]`
//!
//! **P2 视频 Batch（`reserved=1`）：** `[Header] + dict_len + dict + block_count + (每帧: zstd_len + zstd 帧载荷)]`（legacy，无块级校验；**向下兼容**）
//!
//! **P5 嵌入工作流**：`reserved` 最高位 `0x80` 为 1 时，在头后追加 **`META` + u32(zstd 长) + zstd(UTF-8 JSON)**；否则与 Phase P4.1 布局相同。
//!
//! 每帧 zstd 解压后为与单帧相同的明文：`min f32 LE` + `max f32 LE` + `u8` 量化（CHW 序）。

use ndarray::ArrayView1;
use rayon::prelude::*;
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, Write};
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock};
use xxhash_rust::xxh3::xxh3_64;

pub const MAGIC: &[u8; 4] = b"VATS";
pub const FORMAT_VERSION: u16 = 1;
pub const HEADER_LEN: usize = 32;

/// `DctHeader.reserved` **低 7 位**：容器类型 — 0 = 单段 zstd；1 = P2 多块（legacy）；2 = P2 + 每块 XXH3。
///
/// **最高位 (0x80)**：若为 1，则在 32B 头之后存在可选 **`META` 工作流块**（Phase P5），否则与 Phase P4.1 完全一致。
pub const CONTAINER_LEGACY: u8 = 0;
pub const CONTAINER_P2_DICT_BLOCKS: u8 = 1;
/// P2 容器且每个压缩块后带 `u64` XXH3 校验（与 `CONTAINER_P2_DICT_BLOCKS` 除块布局外相同）。
pub const CONTAINER_P2_DICT_BLOCKS_XXH3: u8 = 2;

/// `reserved` 字节中与容器类型独立的 **嵌入工作流 JSON** 标记（与低 7 位 OR）。
pub const RESERVED_FLAG_LBM_EMBEDDED_WORKFLOW_JSON: u8 = 0x80;

/// 工作流元数据块魔数（紧随 32B 头之后，仅在 `reserved & 0x80 != 0` 时出现）。
pub const WORKFLOW_META_MAGIC: &[u8; 4] = b"META";

#[inline]
pub fn container_kind_from_reserved(reserved: u8) -> u8 {
    reserved & 0x7f
}

#[inline]
pub fn reserved_with_workflow_json_flag(container_low7: u8, embed_workflow_json: bool) -> u8 {
    let c = container_low7 & 0x7f;
    if embed_workflow_json {
        c | RESERVED_FLAG_LBM_EMBEDDED_WORKFLOW_JSON
    } else {
        c
    }
}

/// 用于训练字典的最多帧数（控制内存与耗时；大 Batch 只采样前 N 帧）。
const P2_MAX_TRAIN_FRAMES: u32 = 32;
/// 字典最大体积（字节）。
const P2_DICT_MAX_BYTES: usize = 64 * 1024;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct DctHeader {
    pub version: u16,
    pub mode: u8,
    pub reserved: u8,
    pub batch: u32,
    pub channels: u32,
    pub height: u32,
    pub width: u32,
    pub fps: f32,
}

impl DctHeader {
    pub fn new(
        batch: u32,
        channels: u32,
        height: u32,
        width: u32,
        mode: u8,
        fps: f32,
    ) -> Self {
        Self::with_reserved(
            batch,
            channels,
            height,
            width,
            mode,
            fps,
            CONTAINER_LEGACY,
        )
    }

    pub fn with_reserved(
        batch: u32,
        channels: u32,
        height: u32,
        width: u32,
        mode: u8,
        fps: f32,
        reserved: u8,
    ) -> Self {
        Self {
            version: FORMAT_VERSION,
            mode,
            reserved,
            batch,
            channels,
            height,
            width,
            fps,
        }
    }

    pub fn to_bytes_be(&self) -> [u8; HEADER_LEN] {
        let mut out = [0u8; HEADER_LEN];
        out[0..4].copy_from_slice(MAGIC);
        out[4..6].copy_from_slice(&self.version.to_be_bytes());
        out[6] = self.mode;
        out[7] = self.reserved;
        out[8..12].copy_from_slice(&self.batch.to_be_bytes());
        out[12..16].copy_from_slice(&self.channels.to_be_bytes());
        out[16..20].copy_from_slice(&self.height.to_be_bytes());
        out[20..24].copy_from_slice(&self.width.to_be_bytes());
        out[24..28].copy_from_slice(&self.fps.to_be_bytes());
        out
    }

    pub fn from_bytes_be(bytes: &[u8; HEADER_LEN]) -> Result<Self, DctError> {
        if &bytes[0..4] != MAGIC {
            return Err(DctError::BadMagic);
        }
        let version = u16::from_be_bytes([bytes[4], bytes[5]]);
        if version != FORMAT_VERSION {
            return Err(DctError::UnsupportedVersion(version));
        }
        let mode = bytes[6];
        let reserved = bytes[7];
        let batch = u32::from_be_bytes(bytes[8..12].try_into().unwrap());
        let channels = u32::from_be_bytes(bytes[12..16].try_into().unwrap());
        let height = u32::from_be_bytes(bytes[16..20].try_into().unwrap());
        let width = u32::from_be_bytes(bytes[20..24].try_into().unwrap());
        let fps = f32::from_be_bytes(bytes[24..28].try_into().unwrap());

        if batch == 0 || channels == 0 || height == 0 || width == 0 {
            return Err(DctError::InvalidShape {
                batch,
                channels,
                height,
                width,
            });
        }

        Ok(Self {
            version,
            mode,
            reserved,
            batch,
            channels,
            height,
            width,
            fps,
        })
    }

    /// 容器类型（`reserved` 低 7 位，与 P4.1 `CONTAINER_*` 一致）。
    pub fn container_kind(&self) -> u8 {
        container_kind_from_reserved(self.reserved)
    }

    /// 是否在头后带有 **`META` + zstd(JSON)** 工作流块（P5）。
    pub fn has_embedded_workflow_json(&self) -> bool {
        self.reserved & RESERVED_FLAG_LBM_EMBEDDED_WORKFLOW_JSON != 0
    }

    pub fn expected_payload_u8_len(&self) -> Result<usize, DctError> {
        let n = (self.batch as u64)
            .checked_mul(self.channels as u64)
            .and_then(|x| x.checked_mul(self.height as u64))
            .and_then(|x| x.checked_mul(self.width as u64))
            .ok_or(DctError::ShapeOverflow)?;
        usize::try_from(n).map_err(|_| DctError::ShapeOverflow)
    }
}

#[derive(Debug)]
pub enum DctError {
    BadMagic,
    UnsupportedVersion(u16),
    InvalidShape {
        batch: u32,
        channels: u32,
        height: u32,
        width: u32,
    },
    ShapeOverflow,
    UnsupportedBatch(u32),
    Io(std::io::Error),
    Zstd(std::io::Error),
    PayloadTooShort,
    PayloadLenMismatch { expected: usize, got: usize },
    TensorShapeMismatch { expected: usize, got: usize },
    BadP2Container(String),
    BlockCountMismatch { expected: u32, got: u32 },
    /// 解压前检测到压缩块载荷与 **XXH3-64** 不一致（`reserved=2`）。
    DataCorruption { block_index: u32, expected: u64, got: u64 },
}

impl From<std::io::Error> for DctError {
    fn from(e: std::io::Error) -> Self {
        DctError::Io(e)
    }
}

impl std::fmt::Display for DctError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DctError::BadMagic => write!(f, "invalid magic (expected ASCII VATS)"),
            DctError::UnsupportedVersion(v) => write!(f, "unsupported version {v}"),
            DctError::InvalidShape {
                batch,
                channels,
                height,
                width,
            } => write!(
                f,
                "invalid shape in header: B={batch}, C={channels}, H={height}, W={width} (all must be non-zero)"
            ),
            DctError::ShapeOverflow => write!(f, "shape dimensions overflow u64"),
            DctError::UnsupportedBatch(b) => {
                write!(
                    f,
                    "decoded batch B={b}: legacy .dct 仅支持 B=1；多帧请使用 P2 容器（reserved=1 legacy 或 reserved=2 +XXH3）"
                )
            }
            DctError::Io(e) => write!(f, "io: {e}"),
            DctError::Zstd(e) => write!(f, "zstd: {e}"),
            DctError::PayloadTooShort => write!(f, "payload shorter than min/max prefix"),
            DctError::PayloadLenMismatch { expected, got } => {
                write!(f, "payload u8 len mismatch: expected {expected}, got {got}")
            }
            DctError::TensorShapeMismatch { expected, got } => write!(
                f,
                "tensor element count mismatch header B*C*H*W: expected {expected}, got {got}"
            ),
            DctError::BadP2Container(s) => write!(f, "P2 container: {s}"),
            DctError::BlockCountMismatch { expected, got } => write!(
                f,
                "P2 block_count 与头 batch 不一致: expected {expected}, got {got}"
            ),
            DctError::DataCorruption {
                block_index,
                expected,
                got,
            } => write!(
                f,
                "VATES_CORRUPTION: data corruption at P2 block {block_index}: xxh3 expected {expected:#016x}, got {got:#016x}"
            ),
        }
    }
}

impl std::error::Error for DctError {}

fn zstd_encode_mt(src: &[u8], level: i32) -> Result<Vec<u8>, DctError> {
    use std::io::Write;
    let mut enc = zstd::stream::Encoder::new(Vec::new(), level).map_err(DctError::Zstd)?;
    let workers = std::thread::available_parallelism()
        .map(|n| n.get().min(8) as u32)
        .unwrap_or(1);
    if workers > 1 {
        let _ = enc.multithread(workers);
    }
    enc.write_all(src).map_err(DctError::Zstd)?;
    enc.finish().map_err(DctError::Zstd)
}

/// BHWC 展平的一帧 → CHW 展平（均为行主 contiguous）。
fn bhwc_frame_to_chw(src: &[f32], dst: &mut [f32], h: usize, w: usize, c: usize) {
    debug_assert_eq!(src.len(), h * w * c);
    debug_assert_eq!(dst.len(), h * w * c);
    for cc in 0..c {
        for yy in 0..h {
            for xx in 0..w {
                let hwc_i = yy * w * c + xx * c + cc;
                let chw_i = cc * h * w + yy * w + xx;
                dst[chw_i] = src[hwc_i];
            }
        }
    }
}

fn build_uncompressed_payload(min_v: f32, max_v: f32, q: &[u8]) -> Vec<u8> {
    let mut v = Vec::with_capacity(8 + q.len());
    v.extend_from_slice(&min_v.to_le_bytes());
    v.extend_from_slice(&max_v.to_le_bytes());
    v.extend_from_slice(q);
    v
}

fn min_max_par(data: ArrayView1<f32>) -> (f32, f32) {
    data.iter()
        .copied()
        .par_bridge()
        .fold(
            || (f32::INFINITY, f32::NEG_INFINITY),
            |(amin, amax), x| (amin.min(x), amax.max(x)),
        )
        .reduce(
            || (f32::INFINITY, f32::NEG_INFINITY),
            |(a0, a1), (b0, b1)| (a0.min(b0), a1.max(b1)),
        )
}

fn quantize_par(data: ArrayView1<f32>, min_v: f32, max_v: f32) -> Vec<u8> {
    let span = (max_v - min_v).max(f32::EPSILON);
    let len = data.len();
    let mut out = vec![0u8; len];
    out.par_iter_mut()
        .enumerate()
        .for_each(|(i, slot)| {
            let x = data[i];
            let t = ((x - min_v) / span).clamp(0.0, 1.0);
            *slot = (t * 255.0).round() as u8;
        });
    out
}

fn dequantize_par(bytes: &[u8], min_v: f32, max_v: f32) -> Vec<f32> {
    let span = (max_v - min_v).max(f32::EPSILON);
    let len = bytes.len();
    let mut out = vec![0f32; len];
    out.par_iter_mut()
        .enumerate()
        .for_each(|(i, slot)| {
            *slot = min_v + (bytes[i] as f32 / 255.0) * span;
        });
    out
}

fn read_u32_be<R: Read>(r: &mut R) -> Result<u32, DctError> {
    let mut b = [0u8; 4];
    r.read_exact(&mut b).map_err(DctError::Io)?;
    Ok(u32::from_be_bytes(b))
}

fn read_u64_be<R: Read>(r: &mut R) -> Result<u64, DctError> {
    let mut b = [0u8; 8];
    r.read_exact(&mut b).map_err(DctError::Io)?;
    Ok(u64::from_be_bytes(b))
}

#[inline]
fn p2_compressed_xxh3(block: &[u8]) -> u64 {
    xxh3_64(block)
}

fn write_workflow_meta_block<W: Write>(
    w: &mut W,
    json: &str,
    zstd_level: i32,
) -> Result<(), DctError> {
    let z = zstd_encode_mt(json.as_bytes(), zstd_level)?;
    let len_u32 = u32::try_from(z.len()).map_err(|_| DctError::ShapeOverflow)?;
    w.write_all(WORKFLOW_META_MAGIC).map_err(DctError::Io)?;
    w.write_all(&len_u32.to_be_bytes()).map_err(DctError::Io)?;
    w.write_all(&z).map_err(DctError::Io)?;
    Ok(())
}

fn read_workflow_meta_block(f: &mut File) -> Result<String, DctError> {
    let mut magic = [0u8; 4];
    f.read_exact(&mut magic).map_err(DctError::Io)?;
    if &magic != WORKFLOW_META_MAGIC {
        return Err(DctError::BadP2Container(format!(
            "expected ASCII META after header, got {magic:?}"
        )));
    }
    let zlen = read_u32_be(f)? as usize;
    let mut z = vec![0u8; zlen];
    f.read_exact(&mut z).map_err(DctError::Io)?;
    let raw = zstd::decode_all(z.as_slice()).map_err(DctError::Zstd)?;
    String::from_utf8(raw)
        .map_err(|e| DctError::BadP2Container(format!("embedded workflow is not valid UTF-8: {e}")))
}

/// 若头中打了嵌入标记则读取并返回 UTF-8 JSON；否则不读。
fn read_embedded_workflow_json_if_flagged(
    f: &mut File,
    header: &DctHeader,
) -> Result<Option<String>, DctError> {
    if !header.has_embedded_workflow_json() {
        return Ok(None);
    }
    read_workflow_meta_block(f).map(Some)
}

/// 同一 `.dct` 路径串行化（追加与并发写保护）。
fn vates_path_lock(path: &Path) -> Arc<Mutex<()>> {
    static LOCKS: OnceLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> = OnceLock::new();
    let map = LOCKS.get_or_init(|| Mutex::new(HashMap::new()));
    let key = path
        .canonicalize()
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .into_owned();
    let mut g = map.lock().unwrap_or_else(|e| e.into_inner());
    g.entry(key)
        .or_insert_with(|| Arc::new(Mutex::new(())))
        .clone()
}

pub struct Encoder;

impl Encoder {
    /// 单帧（或 B×CHW 展平合一 zstd）— `reserved=0`。
    pub fn encode_file<P: AsRef<Path>>(
        data: ArrayView1<f32>,
        batch: u32,
        channels: u32,
        height: u32,
        width: u32,
        mode: u8,
        fps: f32,
        path: P,
        zstd_level: i32,
        workflow_json: Option<&str>,
    ) -> Result<(), DctError> {
        let path_ref = path.as_ref();
        let lock = vates_path_lock(path_ref);
        let _guard = lock.lock().unwrap_or_else(|e| e.into_inner());

        let expected = (batch as usize)
            .checked_mul(channels as usize)
            .and_then(|x| x.checked_mul(height as usize))
            .and_then(|x| x.checked_mul(width as usize))
            .ok_or(DctError::ShapeOverflow)?;
        if data.len() != expected {
            return Err(DctError::TensorShapeMismatch {
                expected,
                got: data.len(),
            });
        }

        let embed_json = workflow_json.filter(|s| !s.is_empty());
        let reserved =
            reserved_with_workflow_json_flag(CONTAINER_LEGACY, embed_json.is_some());
        let header = DctHeader::with_reserved(
            batch,
            channels,
            height,
            width,
            mode,
            fps,
            reserved,
        );
        let head_bytes = header.to_bytes_be();

        let (min_v, max_v) = min_max_par(data);
        let quantized = quantize_par(data, min_v, max_v);
        let uncompressed = build_uncompressed_payload(min_v, max_v, &quantized);
        let compressed = zstd_encode_mt(uncompressed.as_slice(), zstd_level)?;

        let mut f = File::create(path)?;
        f.write_all(&head_bytes).map_err(DctError::Io)?;
        if let Some(s) = embed_json {
            write_workflow_meta_block(&mut f, s, zstd_level)?;
        }
        f.write_all(&compressed).map_err(DctError::Io)?;
        Ok(())
    }

    /// **P2**：`data_bhwc` 为 C 连续 **BHWC** 展平，`[b,h,w,c]` 序。
    /// `force_p2`：即使 `batch<=1` 仍写 P2 多块容器（供 Streaming 首次创建）。
    /// `header_mode`：写入头字节 mode（如 Video=1、Stream=2）。
    pub fn encode_batch_bhwc_file<P: AsRef<Path>>(
        data_bhwc: &[f32],
        batch: u32,
        height: u32,
        width: u32,
        channels: u32,
        fps: f32,
        path: P,
        zstd_level: i32,
        force_p2: bool,
        header_mode: u8,
        workflow_json: Option<&str>,
    ) -> Result<(), DctError> {
        let path_ref = path.as_ref();
        let lock = vates_path_lock(path_ref);
        let _guard = lock.lock().unwrap_or_else(|e| e.into_inner());

        let h = height as usize;
        let w = width as usize;
        let c = channels as usize;
        let b = batch as usize;

        let frame_hwc = h
            .checked_mul(w)
            .and_then(|x| x.checked_mul(c))
            .ok_or(DctError::ShapeOverflow)?;
        let total = b.checked_mul(frame_hwc).ok_or(DctError::ShapeOverflow)?;
        if data_bhwc.len() != total {
            return Err(DctError::TensorShapeMismatch {
                expected: total,
                got: data_bhwc.len(),
            });
        }

        if batch <= 1 && !force_p2 {
            let mut scratch = vec![0f32; frame_hwc];
            bhwc_frame_to_chw(&data_bhwc[..frame_hwc], &mut scratch, h, w, c);
            let view = ArrayView1::from(scratch.as_slice());
            return Self::encode_file(
                view,
                1,
                channels,
                height,
                width,
                header_mode,
                fps,
                path,
                zstd_level,
                workflow_json,
            );
        }

        let train_n = batch.min(P2_MAX_TRAIN_FRAMES) as usize;
        let uframe = 8usize
            .checked_add(
                (channels as usize)
                    .checked_mul(h)
                    .and_then(|x| x.checked_mul(w))
                    .ok_or(DctError::ShapeOverflow)?,
            )
            .ok_or(DctError::ShapeOverflow)?;

        let mut training = Vec::with_capacity(train_n.saturating_mul(uframe));
        let mut sizes: Vec<usize> = Vec::with_capacity(train_n);
        let mut scratch = vec![0f32; c * h * w];

        for i in 0..train_n {
            let off = i * frame_hwc;
            bhwc_frame_to_chw(&data_bhwc[off..off + frame_hwc], &mut scratch, h, w, c);
            let chw = ArrayView1::from(scratch.as_slice());
            let (min_v, max_v) = min_max_par(chw);
            let q = quantize_par(chw, min_v, max_v);
            let unc = build_uncompressed_payload(min_v, max_v, &q);
            training.extend_from_slice(&unc);
            sizes.push(unc.len());
        }

        if sizes.iter().sum::<usize>() != training.len() {
            return Err(DctError::BadP2Container(
                "internal: training buffer size mismatch".into(),
            ));
        }

        let dict_max = (training.len() / 10)
            .max(1024)
            .min(P2_DICT_MAX_BYTES);
        let dict_raw = zstd::dict::from_continuous(&training, &sizes, dict_max).unwrap_or_default();
        const DICT_MIN_BYTES: usize = 32;
        let use_dict = dict_raw.len() >= DICT_MIN_BYTES;
        let dict_bytes: &[u8] = if use_dict {
            dict_raw.as_slice()
        } else {
            &[]
        };
        let cdict_opt: Option<zstd::dict::EncoderDictionary<'static>> = if use_dict {
            Some(zstd::dict::EncoderDictionary::copy(dict_raw.as_slice(), zstd_level))
        } else {
            None
        };

        let embed_json = workflow_json.filter(|s| !s.is_empty());
        let header = DctHeader::with_reserved(
            batch,
            channels,
            height,
            width,
            header_mode,
            fps,
            reserved_with_workflow_json_flag(CONTAINER_P2_DICT_BLOCKS_XXH3, embed_json.is_some()),
        );
        let mut f = File::create(path)?;
        f.write_all(&header.to_bytes_be()).map_err(DctError::Io)?;
        if let Some(s) = embed_json {
            write_workflow_meta_block(&mut f, s, zstd_level)?;
        }

        let dlen = dict_bytes.len() as u32;
        f.write_all(&dlen.to_be_bytes()).map_err(DctError::Io)?;
        f.write_all(dict_bytes).map_err(DctError::Io)?;
        f.write_all(&batch.to_be_bytes()).map_err(DctError::Io)?;

        use std::io::Write;
        for i in 0..b {
            let off = i * frame_hwc;
            bhwc_frame_to_chw(&data_bhwc[off..off + frame_hwc], &mut scratch, h, w, c);
            let chw = ArrayView1::from(scratch.as_slice());
            let (min_v, max_v) = min_max_par(chw);
            let q = quantize_par(chw, min_v, max_v);
            let unc = build_uncompressed_payload(min_v, max_v, &q);

            let block = if let Some(ref cdict) = cdict_opt {
                let mut enc =
                    zstd::stream::Encoder::with_prepared_dictionary(Vec::new(), cdict)
                        .map_err(DctError::Zstd)?;
                enc.write_all(&unc).map_err(DctError::Zstd)?;
                enc.finish().map_err(DctError::Zstd)?
            } else {
                zstd_encode_mt(unc.as_slice(), zstd_level)?
            };
            let bl = block.len() as u32;
            f.write_all(&bl.to_be_bytes()).map_err(DctError::Io)?;
            f.write_all(&block).map_err(DctError::Io)?;
            let cks = p2_compressed_xxh3(block.as_slice());
            f.write_all(&cks.to_be_bytes()).map_err(DctError::Io)?;
        }

        Ok(())
    }

    /// 向已有 **P2** `.dct` 末尾追加若干 BHWC 帧；更新头内 `batch`（偏移 8–11）与正文中 `block_count`。
    pub fn append_p2_bhwc_frames<P: AsRef<Path>>(
        path: P,
        data_bhwc: &[f32],
        b_new: u32,
        height: u32,
        width: u32,
        channels: u32,
        fps: f32,
        zstd_level: i32,
    ) -> Result<(), DctError> {
        let path_ref = path.as_ref();
        let lock = vates_path_lock(path_ref);
        let _guard = lock.lock().unwrap_or_else(|e| e.into_inner());

        let h = height as usize;
        let w = width as usize;
        let c = channels as usize;
        let bn = b_new as usize;
        let frame_hwc = h
            .checked_mul(w)
            .and_then(|x| x.checked_mul(c))
            .ok_or(DctError::ShapeOverflow)?;
        let need = bn
            .checked_mul(frame_hwc)
            .ok_or(DctError::ShapeOverflow)?;
        if data_bhwc.len() != need {
            return Err(DctError::TensorShapeMismatch {
                expected: need,
                got: data_bhwc.len(),
            });
        }

        // 不能 `append(true)`：在常见 OS 上 append 打开会让写入总是落到文件尾，无法
        // `seek` 回写 header/batch（偏移 8）与正文内 `block_count`。这里用读写打开，
        // 先扫到 EOF，再 `write` 追加压缩块，最后 `seek` 回补丁字段。
        let mut f = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path_ref)
            .map_err(DctError::Io)?;

        let mut head = [0u8; HEADER_LEN];
        f.read_exact(&mut head).map_err(DctError::Io)?;
        let header = DctHeader::from_bytes_be(&head)?;
        let k = header.container_kind();
        if k != CONTAINER_P2_DICT_BLOCKS_XXH3 {
            if k == CONTAINER_P2_DICT_BLOCKS {
                return Err(DctError::BadP2Container(
                    "append 仅支持 container=2（P2+XXH3）；旧版无校验 P2 请重新编码后再流式追加".into(),
                ));
            }
            return Err(DctError::BadP2Container(
                "append 仅支持 P2+XXH3 多块容器（reserved 低 7 位 = 2）".into(),
            ));
        }
        if header.channels != channels || header.height != height || header.width != width {
            return Err(DctError::BadP2Container(
                "追加帧 C/H/W 与文件头不一致".into(),
            ));
        }
        if (header.fps - fps).abs() > 0.01 {
            return Err(DctError::BadP2Container(format!(
                "fps 不一致: file={} new={}",
                header.fps, fps
            )));
        }

        let _workflow_skipped = read_embedded_workflow_json_if_flagged(&mut f, &header)?;

        let dict_len_u32 = read_u32_be(&mut f)?;
        let dict_len = dict_len_u32 as usize;
        let mut dict = vec![0u8; dict_len];
        if dict_len > 0 {
            f.read_exact(&mut dict).map_err(DctError::Io)?;
        }

        let block_count_off = f.stream_position().map_err(DctError::Io)?;
        let old_bc = read_u32_be(&mut f)?;
        for _ in 0..old_bc {
            let zl = read_u32_be(&mut f)? as i64;
            f.seek(std::io::SeekFrom::Current(zl)).map_err(DctError::Io)?;
            f.seek(std::io::SeekFrom::Current(8)).map_err(DctError::Io)?;
        }
        let eof = f.stream_position().map_err(DctError::Io)?;
        let file_len = f.metadata().map_err(DctError::Io)?.len();
        if eof != file_len {
            return Err(DctError::BadP2Container(format!(
                "P2 块区与文件长度不符: pos={eof} len={file_len}"
            )));
        }

        const DICT_MIN: usize = 32;
        let use_dict = dict_len >= DICT_MIN;
        let cdict_opt: Option<zstd::dict::EncoderDictionary<'static>> = if use_dict {
            Some(zstd::dict::EncoderDictionary::copy(&dict, zstd_level))
        } else {
            None
        };

        let mut scratch = vec![0f32; c * h * w];
        for i in 0..bn {
            let off = i * frame_hwc;
            bhwc_frame_to_chw(
                &data_bhwc[off..off + frame_hwc],
                &mut scratch,
                h,
                w,
                c,
            );
            let chw = ArrayView1::from(scratch.as_slice());
            let (min_v, max_v) = min_max_par(chw);
            let q = quantize_par(chw, min_v, max_v);
            let unc = build_uncompressed_payload(min_v, max_v, &q);
            let block = if let Some(ref cdict) = cdict_opt {
                let mut enc = zstd::stream::Encoder::with_prepared_dictionary(Vec::new(), cdict)
                    .map_err(DctError::Zstd)?;
                enc.write_all(&unc).map_err(DctError::Zstd)?;
                enc.finish().map_err(DctError::Zstd)?
            } else {
                zstd_encode_mt(unc.as_slice(), zstd_level)?
            };
            f.write_all(&(block.len() as u32).to_be_bytes())
                .map_err(DctError::Io)?;
            f.write_all(&block).map_err(DctError::Io)?;
            let cks = p2_compressed_xxh3(block.as_slice());
            f.write_all(&cks.to_be_bytes()).map_err(DctError::Io)?;
        }

        let new_bc = old_bc.checked_add(b_new).ok_or(DctError::ShapeOverflow)?;
        let new_batch = header
            .batch
            .checked_add(b_new)
            .ok_or(DctError::ShapeOverflow)?;
        f.seek(std::io::SeekFrom::Start(block_count_off))
            .map_err(DctError::Io)?;
        f.write_all(&new_bc.to_be_bytes()).map_err(DctError::Io)?;

        f.seek(std::io::SeekFrom::Start(8)).map_err(DctError::Io)?;
        f.write_all(&new_batch.to_be_bytes()).map_err(DctError::Io)?;

        f.sync_all().map_err(DctError::Io)?;
        Ok(())
    }
}

/// 解码结果：图像张量载荷 + 可选嵌入的 ComfyUI 工作流 JSON（Phase P5）。
#[derive(Debug, Clone)]
pub struct DecodedDct {
    pub header: DctHeader,
    pub floats: Vec<f32>,
    pub workflow_json: Option<String>,
}

pub struct Decoder;

impl Decoder {
    /// 仅读取 32 字节 VATS 头（不解码正文）。
    pub fn read_header_only<P: AsRef<Path>>(path: P) -> Result<DctHeader, DctError> {
        let mut f = File::open(path)?;
        let mut head = [0u8; HEADER_LEN];
        f.read_exact(&mut head)?;
        DctHeader::from_bytes_be(&head)
    }

    /// 只读取嵌入的工作流 JSON（若有）；无 `0x80` 标记时返回 `Ok(None)`。
    pub fn read_embedded_workflow_json<P: AsRef<Path>>(path: P) -> Result<Option<String>, DctError> {
        let mut f = File::open(path.as_ref())?;
        let mut head = [0u8; HEADER_LEN];
        f.read_exact(&mut head)?;
        let header = DctHeader::from_bytes_be(&head)?;
        read_embedded_workflow_json_if_flagged(&mut f, &header)
    }

    pub fn decode_file<P: AsRef<Path>>(path: P) -> Result<(DctHeader, Vec<f32>), DctError> {
        let d = Self::decode_file_full(path)?;
        Ok((d.header, d.floats))
    }

    pub fn decode_file_full<P: AsRef<Path>>(path: P) -> Result<DecodedDct, DctError> {
        let mut f = File::open(path.as_ref())?;
        let mut head = [0u8; HEADER_LEN];
        f.read_exact(&mut head)?;
        let header = DctHeader::from_bytes_be(&head)?;
        let workflow_json = read_embedded_workflow_json_if_flagged(&mut f, &header)?;

        let (hdr, floats) = match header.container_kind() {
            CONTAINER_LEGACY => Self::decode_legacy_body(&header, &mut f)?,
            CONTAINER_P2_DICT_BLOCKS => Self::decode_p2_blocks(&header, &mut f, false)?,
            CONTAINER_P2_DICT_BLOCKS_XXH3 => Self::decode_p2_blocks(&header, &mut f, true)?,
            _ => {
                return Err(DctError::BadP2Container(format!(
                    "unknown container kind {} (reserved byte {})",
                    header.container_kind(),
                    header.reserved
                )));
            }
        };
        Ok(DecodedDct {
            header: hdr,
            floats,
            workflow_json,
        })
    }

    fn decode_legacy_body<R: Read>(header: &DctHeader, r: &mut R) -> Result<(DctHeader, Vec<f32>), DctError> {
        if header.batch != 1 {
            return Err(DctError::UnsupportedBatch(header.batch));
        }

        let mut body = Vec::new();
        r.read_to_end(&mut body)?;
        let raw = zstd::decode_all(body.as_slice()).map_err(DctError::Zstd)?;
        if raw.len() < 8 {
            return Err(DctError::PayloadTooShort);
        }
        let min_v = f32::from_le_bytes(raw[0..4].try_into().unwrap());
        let max_v = f32::from_le_bytes(raw[4..8].try_into().unwrap());
        let u8_slice = &raw[8..];
        let expected = header.expected_payload_u8_len()?;
        if u8_slice.len() != expected {
            return Err(DctError::PayloadLenMismatch {
                expected,
                got: u8_slice.len(),
            });
        }

        let floats = dequantize_par(u8_slice, min_v, max_v);
        if floats.len() != expected {
            return Err(DctError::PayloadLenMismatch {
                expected,
                got: floats.len(),
            });
        }
        Ok((*header, floats))
    }

    fn decode_p2_blocks<R: Read>(
        header: &DctHeader,
        r: &mut R,
        xxh3_after_each_block: bool,
    ) -> Result<(DctHeader, Vec<f32>), DctError> {
        let dict_len_u32 = read_u32_be(r)?;
        let dict_len = dict_len_u32 as usize;
        let mut dict = vec![0u8; dict_len];
        if dict_len > 0 {
            r.read_exact(&mut dict).map_err(DctError::Io)?;
        }

        let block_count = read_u32_be(r)?;
        if block_count != header.batch {
            return Err(DctError::BlockCountMismatch {
                expected: header.batch,
                got: block_count,
            });
        }

        let ddict_opt = if dict_len >= 32 {
            Some(zstd::dict::DecoderDictionary::copy(&dict))
        } else {
            None
        };
        let mut dctx_opt = match &ddict_opt {
            Some(d) => Some(zstd::bulk::Decompressor::with_prepared_dictionary(d).map_err(DctError::Zstd)?),
            None => None,
        };

        let frame_u8 = (header.channels as usize)
            .checked_mul(header.height as usize)
            .and_then(|x| x.checked_mul(header.width as usize))
            .ok_or(DctError::ShapeOverflow)?;
        let uframe = 8usize.checked_add(frame_u8).ok_or(DctError::ShapeOverflow)?;

        let total_f_usize = header.expected_payload_u8_len()?;

        let mut all = Vec::with_capacity(total_f_usize);

        for block_idx in 0..block_count {
            let zlen = read_u32_be(r)? as usize;
            let mut zbuf = vec![0u8; zlen];
            r.read_exact(&mut zbuf).map_err(DctError::Io)?;

            if xxh3_after_each_block {
                let expected = read_u64_be(r)?;
                let got = p2_compressed_xxh3(zbuf.as_slice());
                if got != expected {
                    return Err(DctError::DataCorruption {
                        block_index: block_idx,
                        expected,
                        got,
                    });
                }
            }

            let raw = if let Some(ref mut dctx) = dctx_opt {
                dctx.decompress(&zbuf, uframe).map_err(DctError::Zstd)?
            } else {
                zstd::decode_all(zbuf.as_slice()).map_err(DctError::Zstd)?
            };
            if raw.len() != uframe {
                return Err(DctError::PayloadLenMismatch {
                    expected: uframe,
                    got: raw.len(),
                });
            }
            let min_v = f32::from_le_bytes(raw[0..4].try_into().unwrap());
            let max_v = f32::from_le_bytes(raw[4..8].try_into().unwrap());
            let u8s = &raw[8..];
            if u8s.len() != frame_u8 {
                return Err(DctError::PayloadLenMismatch {
                    expected: frame_u8,
                    got: u8s.len(),
                });
            }
            let floats = dequantize_par(u8s, min_v, max_v);
            all.extend(floats);
        }

        if all.len() != total_f_usize {
            return Err(DctError::PayloadLenMismatch {
                expected: total_f_usize,
                got: all.len(),
            });
        }

        Ok((*header, all))
    }

    /// 全量校验：legacy 校验整段 zstd；P2(`reserved=1`) 结构 + 逐块解压；P2+XXH3(`reserved=2`) 含每块 **XXH3** 与解压。
    pub fn verify_file<P: AsRef<Path>>(path: P) -> Result<String, DctError> {
        let mut f = File::open(path.as_ref())?;
        let mut head = [0u8; HEADER_LEN];
        f.read_exact(&mut head)?;
        let header = DctHeader::from_bytes_be(&head)?;
        let _meta = read_embedded_workflow_json_if_flagged(&mut f, &header)?;
        match header.container_kind() {
            CONTAINER_LEGACY => {
                let mut body = Vec::new();
                f.read_to_end(&mut body).map_err(DctError::Io)?;
                zstd::decode_all(body.as_slice()).map_err(DctError::Zstd)?;
                Ok(format!(
                    "legacy: OK — zstd payload {} bytes",
                    body.len()
                ))
            }
            CONTAINER_P2_DICT_BLOCKS | CONTAINER_P2_DICT_BLOCKS_XXH3 => {
                let xxh3 = header.container_kind() == CONTAINER_P2_DICT_BLOCKS_XXH3;
                Self::verify_p2_streaming(&header, &mut f, xxh3)
            }
            other => Err(DctError::BadP2Container(format!(
                "unknown container kind {other}"
            ))),
        }
    }

    fn verify_p2_streaming(
        header: &DctHeader,
        f: &mut File,
        xxh3_trailer: bool,
    ) -> Result<String, DctError> {
        let dict_len = read_u32_be(f)? as usize;
        let mut dict = vec![0u8; dict_len];
        if dict_len > 0 {
            f.read_exact(&mut dict).map_err(DctError::Io)?;
        }
        let block_count = read_u32_be(f)?;
        if block_count != header.batch {
            return Err(DctError::BlockCountMismatch {
                expected: header.batch,
                got: block_count,
            });
        }
        let ddict_opt = if dict_len >= 32 {
            Some(zstd::dict::DecoderDictionary::copy(&dict))
        } else {
            None
        };
        let mut dctx_opt = match &ddict_opt {
            Some(d) => {
                Some(zstd::bulk::Decompressor::with_prepared_dictionary(d).map_err(DctError::Zstd)?)
            }
            None => None,
        };
        let frame_u8 = (header.channels as usize)
            .checked_mul(header.height as usize)
            .and_then(|x| x.checked_mul(header.width as usize))
            .ok_or(DctError::ShapeOverflow)?;
        let uframe = 8usize.checked_add(frame_u8).ok_or(DctError::ShapeOverflow)?;

        for block_idx in 0..block_count {
            let zlen = read_u32_be(f)? as usize;
            let mut zbuf = vec![0u8; zlen];
            f.read_exact(&mut zbuf).map_err(DctError::Io)?;
            if xxh3_trailer {
                let expected = read_u64_be(f)?;
                let got = p2_compressed_xxh3(zbuf.as_slice());
                if got != expected {
                    return Err(DctError::DataCorruption {
                        block_index: block_idx,
                        expected,
                        got,
                    });
                }
            }
            let raw = if let Some(ref mut dctx) = dctx_opt {
                dctx.decompress(&zbuf, uframe).map_err(DctError::Zstd)?
            } else {
                zstd::decode_all(zbuf.as_slice()).map_err(DctError::Zstd)?
            };
            if raw.len() != uframe {
                return Err(DctError::PayloadLenMismatch {
                    expected: uframe,
                    got: raw.len(),
                });
            }
        }

        let pos = f.stream_position().map_err(DctError::Io)?;
        let len = f.metadata().map_err(DctError::Io)?.len();
        if pos != len {
            return Err(DctError::BadP2Container(format!(
                "P2 块区之后仍有冗余数据: pos={pos}, file_len={len}"
            )));
        }

        Ok(if xxh3_trailer {
            format!("P2+XXH3: OK — {block_count} blocks (checksum + zstd)")
        } else {
            format!("P2 legacy: OK — {block_count} blocks (no per-block checksum)")
        })
    }
}

pub fn synthetic_chw(c: usize, h: usize, w: usize) -> ndarray::Array1<f32> {
    let len = c * h * w;
    ndarray::Array1::from_shape_fn(len, |i| {
        let i = i as f32;
        (i * 0.001).sin() * 0.5 + 0.5
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_roundtrip_be_bytes() {
        let h = DctHeader::new(1, 3, 1080, 1920, 1, 24.5);
        let b = h.to_bytes_be();
        let h2 = DctHeader::from_bytes_be(&b).unwrap();
        assert_eq!(h.batch, h2.batch);
        assert_eq!(h.channels, h2.channels);
        assert_eq!(h.height, h2.height);
        assert_eq!(h.width, h2.width);
        assert_eq!(h.mode, h2.mode);
        assert_eq!(h.fps.to_bits(), h2.fps.to_bits());
    }

    #[test]
    fn encode_decode_roundtrip() {
        let w = 64u32;
        let h = 64u32;
        let c = 3u32;
        let data = synthetic_chw(c as usize, h as usize, w as usize);
        let dir = std::env::temp_dir();
        let path = dir.join("vates_test_vats.dct");
        Encoder::encode_file(data.view(), 1, c, h, w, 0, 24.0, &path, 3, None).unwrap();
        let (hdr, out) = Decoder::decode_file(&path).unwrap();
        assert_eq!(hdr.width, w);
        assert_eq!(hdr.height, h);
        assert_eq!(hdr.channels, c);
        assert_eq!(hdr.batch, 1);
        assert_eq!(hdr.reserved, CONTAINER_LEGACY);
        assert_eq!(out.len(), data.len());
        for (a, b) in data.iter().zip(out.iter()) {
            assert!((a - b).abs() < 1e-2);
        }
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn p2_batch_roundtrip() {
        let b = 4u32;
        let h = 32usize;
        let w = 32usize;
        let c = 3usize;
        let frame = h * w * c;
        let mut bhwc = vec![0f32; b as usize * frame];
        for bi in 0..b as usize {
            let chw = synthetic_chw(c, h, w);
            for (idx, &val) in chw.iter().enumerate() {
                let cc = idx / (h * w);
                let rem = idx % (h * w);
                let yy = rem / w;
                let xx = rem % w;
                let bhwc_i = bi * frame + yy * w * c + xx * c + cc;
                bhwc[bhwc_i] = val + bi as f32 * 0.01;
            }
        }
        let path = std::env::temp_dir().join("vates_p2.dct");
        Encoder::encode_batch_bhwc_file(
            &bhwc, b, h as u32, w as u32, c as u32, 24.0, &path, 3, false, 1, None,
        )
            .unwrap();
        let (hdr, out) = Decoder::decode_file(&path).unwrap();
        assert_eq!(hdr.batch, b);
        assert_eq!(hdr.reserved, CONTAINER_P2_DICT_BLOCKS_XXH3);
        assert_eq!(out.len(), b as usize * frame);
        let rpt = Decoder::verify_file(&path).unwrap();
        assert!(rpt.contains("XXH3"), "{rpt}");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn p2_append_roundtrip() {
        let h = 16usize;
        let w = 16usize;
        let c = 3usize;
        let frame = h * w * c;
        let b0 = 2u32;
        let mut bhwc0 = vec![0f32; b0 as usize * frame];
        for bi in 0..b0 as usize {
            let chw = synthetic_chw(c, h, w);
            for (idx, &val) in chw.iter().enumerate() {
                let cc = idx / (h * w);
                let rem = idx % (h * w);
                let yy = rem / w;
                let xx = rem % w;
                let bhwc_i = bi * frame + yy * w * c + xx * c + cc;
                bhwc0[bhwc_i] = val + bi as f32 * 0.02;
            }
        }
        let path = std::env::temp_dir().join("vates_p2_append.dct");
        Encoder::encode_batch_bhwc_file(
            &bhwc0, b0, h as u32, w as u32, c as u32, 30.0, &path, 3, false, 1, None,
        )
        .unwrap();

        let b_new = 2u32;
        let mut more = vec![0f32; b_new as usize * frame];
        for bi in 0..b_new as usize {
            let chw = synthetic_chw(c, h, w);
            for (idx, &val) in chw.iter().enumerate() {
                let cc = idx / (h * w);
                let rem = idx % (h * w);
                let yy = rem / w;
                let xx = rem % w;
                let bhwc_i = bi * frame + yy * w * c + xx * c + cc;
                more[bhwc_i] = val + (bi + 10) as f32 * 0.03;
            }
        }
        Encoder::append_p2_bhwc_frames(&path, &more, b_new, h as u32, w as u32, c as u32, 30.0, 3)
            .unwrap();

        let (hdr, out) = Decoder::decode_file(&path).unwrap();
        assert_eq!(hdr.batch, b0 + b_new);
        assert_eq!(hdr.reserved, CONTAINER_P2_DICT_BLOCKS_XXH3);
        assert_eq!(out.len(), (b0 + b_new) as usize * frame);
        let rpt = Decoder::verify_file(&path).unwrap();
        assert!(rpt.contains("XXH3"), "{rpt}");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn p2_embedded_workflow_json_roundtrip() {
        let b = 2u32;
        let h = 8usize;
        let w = 8usize;
        let c = 3usize;
        let frame = h * w * c;
        let mut bhwc = vec![0f32; b as usize * frame];
        for bi in 0..b as usize {
            let chw = synthetic_chw(c, h, w);
            for (idx, &val) in chw.iter().enumerate() {
                let cc = idx / (h * w);
                let rem = idx % (h * w);
                let yy = rem / w;
                let xx = rem % w;
                let bhwc_i = bi * frame + yy * w * c + xx * c + cc;
                bhwc[bhwc_i] = val;
            }
        }
        let path = std::env::temp_dir().join("vates_p5_wf.dct");
        let wf = r#"{"prompt":{"1":{"class_type":"Test"}},"extra_pnginfo":{}}"#;
        Encoder::encode_batch_bhwc_file(
            &bhwc,
            b,
            h as u32,
            w as u32,
            c as u32,
            24.0,
            &path,
            3,
            false,
            1,
            Some(wf),
        )
        .unwrap();
        let d = Decoder::decode_file_full(&path).unwrap();
        assert!(d.header.has_embedded_workflow_json());
        assert_eq!(
            d.header.reserved,
            reserved_with_workflow_json_flag(CONTAINER_P2_DICT_BLOCKS_XXH3, true)
        );
        assert_eq!(d.workflow_json.as_deref(), Some(wf));
        assert_eq!(d.header.batch, b);
        let peek = Decoder::read_embedded_workflow_json(&path).unwrap();
        assert_eq!(peek.as_deref(), Some(wf));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn p2_xxh3_rejects_tampering() {
        let b = 2u32;
        let h = 8usize;
        let w = 8usize;
        let c = 3usize;
        let frame = h * w * c;
        let mut bhwc = vec![0f32; b as usize * frame];
        for bi in 0..b as usize {
            let chw = synthetic_chw(c, h, w);
            for (idx, &val) in chw.iter().enumerate() {
                let cc = idx / (h * w);
                let rem = idx % (h * w);
                let yy = rem / w;
                let xx = rem % w;
                let bhwc_i = bi * frame + yy * w * c + xx * c + cc;
                bhwc[bhwc_i] = val;
            }
        }
        let path = std::env::temp_dir().join("vates_p2_tamper.dct");
        Encoder::encode_batch_bhwc_file(
            &bhwc, b, h as u32, w as u32, c as u32, 24.0, &path, 3, false, 1, None,
        )
        .unwrap();
        let mut bytes = std::fs::read(&path).unwrap();
        let dict_len = u32::from_be_bytes(bytes[HEADER_LEN..HEADER_LEN + 4].try_into().unwrap()) as usize;
        let z0_off = HEADER_LEN + 4 + dict_len + 4;
        assert!(z0_off + 8 <= bytes.len());
        let z0_len = u32::from_be_bytes(bytes[z0_off..z0_off + 4].try_into().unwrap()) as usize;
        let flip_at = z0_off + 4 + (z0_len / 2).max(1);
        assert!(
            flip_at < z0_off + 4 + z0_len,
            "flip must land inside first zstd payload"
        );
        bytes[flip_at] ^= 0x55;
        std::fs::write(&path, &bytes).unwrap();
        let err = Decoder::decode_file(&path).unwrap_err();
        let msg = err.to_string();
        assert!(
            matches!(err, DctError::DataCorruption { .. }) || msg.contains("VATES_CORRUPTION"),
            "expected corruption error, got: {msg}"
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn rejects_bad_magic() {
        let mut buf = [0u8; HEADER_LEN];
        buf[0..4].copy_from_slice(b"XXXX");
        assert!(matches!(
            DctHeader::from_bytes_be(&buf),
            Err(DctError::BadMagic)
        ));
    }

    #[test]
    fn file_starts_with_vats_magic() {
        let w = 8u32;
        let h = 8u32;
        let c = 3u32;
        let data = synthetic_chw(c as usize, h as usize, w as usize);
        let path = std::env::temp_dir().join("vates_magic_test.dct");
        Encoder::encode_file(data.view(), 1, c, h, w, 2, 30.0, &path, 3, None).unwrap();
        let mut four = [0u8; 4];
        let mut f = File::open(&path).unwrap();
        f.read_exact(&mut four).unwrap();
        assert_eq!(&four, MAGIC);
        let _ = std::fs::remove_file(path);
    }
}
