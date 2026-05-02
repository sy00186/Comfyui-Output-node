//! Vates `.dct`：16 字节 LE 文件头 + zstd 压缩载荷。
//!
//! **解压后明文载荷布局：**
//! - `[0..4]`：`min`（`f32`，LE）
//! - `[4..8]`：`max`（`f32`，LE）
//! - `[8..]`：逐元素 `u8` 量化数据，长度 `width * height * channels`

use ndarray::ArrayView1;
use rayon::prelude::*;
use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;

pub const MAGIC: &[u8; 4] = b"DCT_";
pub const FORMAT_VERSION: u16 = 1;
pub const HEADER_LEN: usize = 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DctHeader {
    pub magic: [u8; 4],
    pub version: u16,
    pub width: u32,
    pub height: u32,
    pub channels: u8,
    pub flags: u8,
}

impl DctHeader {
    pub fn new(width: u32, height: u32, channels: u8, flags: u8) -> Self {
        Self {
            magic: *MAGIC,
            version: FORMAT_VERSION,
            width,
            height,
            channels,
            flags,
        }
    }

    pub fn to_bytes_le(&self) -> [u8; HEADER_LEN] {
        let mut out = [0u8; HEADER_LEN];
        out[0..4].copy_from_slice(&self.magic);
        out[4..6].copy_from_slice(&self.version.to_le_bytes());
        out[6..10].copy_from_slice(&self.width.to_le_bytes());
        out[10..14].copy_from_slice(&self.height.to_le_bytes());
        out[14] = self.channels;
        out[15] = self.flags;
        out
    }

    pub fn from_bytes_le(bytes: &[u8; HEADER_LEN]) -> Result<Self, DctError> {
        if &bytes[0..4] != MAGIC {
            return Err(DctError::BadMagic);
        }
        let version = u16::from_le_bytes([bytes[4], bytes[5]]);
        if version != FORMAT_VERSION {
            return Err(DctError::UnsupportedVersion(version));
        }
        let width = u32::from_le_bytes(bytes[6..10].try_into().unwrap());
        let height = u32::from_le_bytes(bytes[10..14].try_into().unwrap());
        let channels = bytes[14];
        let flags = bytes[15];
        Ok(Self {
            magic: *MAGIC,
            version,
            width,
            height,
            channels,
            flags,
        })
    }

    pub fn expected_payload_u8_len(&self) -> usize {
        let n = (self.width as u64)
            .saturating_mul(self.height as u64)
            .saturating_mul(self.channels as u64);
        usize::try_from(n).unwrap_or(usize::MAX)
    }
}

#[derive(Debug)]
pub enum DctError {
    BadMagic,
    UnsupportedVersion(u16),
    Io(std::io::Error),
    Zstd(std::io::Error),
    PayloadTooShort,
    PayloadLenMismatch { expected: usize, got: usize },
}

impl From<std::io::Error> for DctError {
    fn from(e: std::io::Error) -> Self {
        DctError::Io(e)
    }
}

impl std::fmt::Display for DctError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DctError::BadMagic => write!(f, "invalid magic (expected DCT_)"),
            DctError::UnsupportedVersion(v) => write!(f, "unsupported version {v}"),
            DctError::Io(e) => write!(f, "io: {e}"),
            DctError::Zstd(e) => write!(f, "zstd: {e}"),
            DctError::PayloadTooShort => write!(f, "payload shorter than min/max prefix"),
            DctError::PayloadLenMismatch { expected, got } => {
                write!(f, "payload u8 len mismatch: expected {expected}, got {got}")
            }
        }
    }
}

impl std::error::Error for DctError {}

pub struct Encoder;

impl Encoder {
    pub fn encode_file<P: AsRef<Path>>(
        data: ArrayView1<f32>,
        width: u32,
        height: u32,
        channels: u8,
        flags: u8,
        path: P,
        zstd_level: i32,
    ) -> Result<(), DctError> {
        let expected = (width as usize)
            .saturating_mul(height as usize)
            .saturating_mul(channels as usize);
        if data.len() != expected {
            return Err(DctError::PayloadLenMismatch {
                expected,
                got: data.len(),
            });
        }

        let (min_v, max_v) = min_max_par(data);
        let quantized = quantize_par(data, min_v, max_v);
        let uncompressed = build_uncompressed_payload(min_v, max_v, &quantized);
        let compressed = zstd::encode_all(uncompressed.as_slice(), zstd_level)
            .map_err(DctError::Zstd)?;

        let header = DctHeader::new(width, height, channels, flags);
        let mut f = File::create(path)?;
        f.write_all(&header.to_bytes_le())?;
        f.write_all(&compressed)?;
        Ok(())
    }
}

pub struct Decoder;

impl Decoder {
    pub fn decode_file<P: AsRef<Path>>(path: P) -> Result<(DctHeader, Vec<f32>), DctError> {
        let mut f = File::open(path)?;
        let mut head = [0u8; HEADER_LEN];
        f.read_exact(&mut head)?;
        let header = DctHeader::from_bytes_le(&head)?;

        let mut body = Vec::new();
        f.read_to_end(&mut body)?;
        let raw = zstd::decode_all(body.as_slice()).map_err(DctError::Zstd)?;
        if raw.len() < 8 {
            return Err(DctError::PayloadTooShort);
        }
        let min_v = f32::from_le_bytes(raw[0..4].try_into().unwrap());
        let max_v = f32::from_le_bytes(raw[4..8].try_into().unwrap());
        let u8_slice = &raw[8..];
        let expected = header.expected_payload_u8_len();
        if u8_slice.len() != expected {
            return Err(DctError::PayloadLenMismatch {
                expected,
                got: u8_slice.len(),
            });
        }

        let floats = dequantize_par(u8_slice, min_v, max_v);
        Ok((header, floats))
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
    (0..len)
        .into_par_iter()
        .map(|i| {
            let x = data[i];
            let t = ((x - min_v) / span).clamp(0.0, 1.0);
            (t * 255.0).round() as u8
        })
        .collect()
}

fn dequantize_par(bytes: &[u8], min_v: f32, max_v: f32) -> Vec<f32> {
    let span = (max_v - min_v).max(f32::EPSILON);
    bytes
        .par_iter()
        .map(|&b| min_v + (b as f32 / 255.0) * span)
        .collect()
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
    fn header_roundtrip_bytes() {
        let h = DctHeader::new(1920, 1080, 3, 0);
        let b = h.to_bytes_le();
        let h2 = DctHeader::from_bytes_le(&b).unwrap();
        assert_eq!(h.width, h2.width);
        assert_eq!(h.height, h2.height);
        assert_eq!(h.channels, h2.channels);
        assert_eq!(h.flags, h2.flags);
    }

    #[test]
    fn encode_decode_roundtrip() {
        let w = 64u32;
        let h = 64u32;
        let c = 3u8;
        let data = synthetic_chw(c as usize, h as usize, w as usize);
        let dir = std::env::temp_dir();
        let path = dir.join("vates_test.dct");
        Encoder::encode_file(data.view(), w, h, c, 0, &path, 3).unwrap();
        let (hdr, out) = Decoder::decode_file(&path).unwrap();
        assert_eq!(hdr.width, w);
        assert_eq!(hdr.height, h);
        assert_eq!(hdr.channels, c);
        assert_eq!(out.len(), data.len());
        for (a, b) in data.iter().zip(out.iter()) {
            assert!((a - b).abs() < 1e-2);
        }
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn rejects_bad_magic() {
        let mut buf = [0u8; HEADER_LEN];
        buf[0..4].copy_from_slice(b"XXXX");
        assert!(matches!(
            DctHeader::from_bytes_le(&buf),
            Err(DctError::BadMagic)
        ));
    }
}
