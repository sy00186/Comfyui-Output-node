//! CLI smoke test for `vates` binary (`.dct` POC).

use vates_core::{Decoder, Encoder, synthetic_chw};
use std::env;
use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args().skip(1);
    let cmd = args.next().unwrap_or_else(|| "help".into());

    match cmd.as_str() {
        "encode" => {
            let out = args.next().map(PathBuf::from).unwrap_or_else(|| PathBuf::from("out.dct"));
            let w = 512u32;
            let h = 512u32;
            let c = 3u8;
            let data = synthetic_chw(c as usize, h as usize, w as usize);
            Encoder::encode_file(data.view(), w, h, c, 0, &out, 3)?;
            println!("wrote {}", out.display());
        }
        "decode" => {
            let path = args.next().map(PathBuf::from).expect("usage: decode <file.dct>");
            let (hdr, _) = Decoder::decode_file(&path)?;
            println!(
                "{}x{} c={} flags={:#04x}",
                hdr.width, hdr.height, hdr.channels, hdr.flags
            );
        }
        _ => {
            eprintln!("usage: vates encode [out.dct] | decode <file.dct>");
        }
    }
    Ok(())
}
