use criterion::{black_box, criterion_group, criterion_main, Criterion, Throughput};
use vates_core::{Decoder, Encoder, synthetic_chw};
use std::hint::black_box as bb;

fn bench_roundtrip(c: &mut Criterion) {
    let w = 512u32;
    let h = 512u32;
    let ch = 3u8;
    let data = synthetic_chw(ch as usize, h as usize, w as usize);
    let bytes = (data.len() * 4) as u64;
    let mut group = c.benchmark_group("dct");
    group.throughput(Throughput::Bytes(bytes));

    group.bench_function("encode_zstd3", |b| {
        b.iter(|| {
            Encoder::encode_file(
                data.view(),
                1,
                bb(ch as u32),
                bb(h),
                bb(w),
                0,
                24.0,
                std::env::temp_dir().join("__dct_bench_encode.dct"),
                3,
                None,
            )
            .unwrap();
        });
    });

    let tmp = std::env::temp_dir().join("__dct_bench_roundtrip.dct");
    Encoder::encode_file(data.view(), 1, ch as u32, h, w, 0, 24.0, &tmp, 3, None).unwrap();

    group.bench_function("decode", |b| {
        b.iter(|| {
            let (_hdr, _v) = Decoder::decode_file(black_box(&tmp)).unwrap();
        });
    });

    group.finish();
    let _ = std::fs::remove_file(tmp);
}

criterion_group!(benches, bench_roundtrip);
criterion_main!(benches);
