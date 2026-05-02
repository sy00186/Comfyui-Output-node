//! Vates 命令行：`inspect` / `verify`（不依赖 Python）。

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;
use vates_core::{Decoder, DctError, Encoder, FORMAT_VERSION, synthetic_chw};

fn print_help() {
    eprintln!(
        "Vates CLI — .dct 工具\n\
         \n\
         用法:\n\
           vates inspect [--json] <file.dct>  打印头表；`--json` 仅输出嵌入的工作流 JSON（若有）\n\
           vates verify <file.dct>      全量校验（P2+XXH3 验证每块哈希；legacy/P2 校验 zstd）\n\
           vates encode [out.dct]       生成单帧测试文件（POC）\n\
           vates decode <file.dct>      解码并打印 B/C/H/W（POC）\n\
         \n\
         示例:\n\
           cargo run --no-default-features --bin vates -- inspect ./out.dct\n\
           cargo run --no-default-features --bin vates -- verify ./out.dct"
    );
}

fn main() -> ExitCode {
    let mut args = env::args().skip(1);
    let cmd = args.next().unwrap_or_else(|| "help".into());

    let mut run = || -> Result<(), Box<dyn std::error::Error>> {
        match cmd.as_str() {
            "inspect" => {
                let mut json_only = false;
                let mut path_opt: Option<PathBuf> = None;
                while let Some(a) = args.next() {
                    if a == "--json" {
                        json_only = true;
                    } else if path_opt.is_none() {
                        path_opt = Some(PathBuf::from(a));
                    } else {
                        return Err(format!("未知参数: {a}").into());
                    }
                }
                let path = path_opt.ok_or("用法: vates inspect [--json] <file.dct>")?;
                if json_only {
                    match Decoder::read_embedded_workflow_json(&path)? {
                        Some(s) => println!("{s}"),
                        None => eprintln!("(无嵌入工作流 JSON：reserved 未置 0x80 或文件无 META 块)"),
                    }
                } else {
                let h = Decoder::read_header_only(&path)?;
                let ck = h.container_kind();
                let container = match ck {
                    vates_core::CONTAINER_LEGACY => "legacy (单段 zstd)",
                    vates_core::CONTAINER_P2_DICT_BLOCKS => "P2 多块（无块级 XXH3）",
                    vates_core::CONTAINER_P2_DICT_BLOCKS_XXH3 => "P2 多块 + XXH3（每块 u64）",
                    x => return Err(format!("未知 container 低 7 位={x}").into()),
                };
                println!("┌─────────────────┬──────────────────────────────┐");
                println!("│ 字段            │ 值                             │");
                println!("├─────────────────┼──────────────────────────────┤");
                println!("│ Magic           │ VATS                           │");
                println!(
                    "│ Version (u16)   │ {:<30} │",
                    format!("{}  (FORMAT_VERSION={})", h.version, FORMAT_VERSION)
                );
                println!("│ Mode (u8)       │ {:<30} │", h.mode);
                println!("│ Reserved (raw)  │ {:<30} │", h.reserved);
                println!(
                    "│ Container (低7) │ {:<30} │",
                    format!("{} → {}", ck, container)
                );
                println!(
                    "│ 嵌入工作流 JSON │ {:<30} │",
                    if h.has_embedded_workflow_json() {
                        "是 (META+zstd)"
                    } else {
                        "否"
                    }
                );
                println!("│ Batch (B)       │ {:<30} │", h.batch);
                println!("│ Channels (C)    │ {:<30} │", h.channels);
                println!("│ Height (H)      │ {:<30} │", h.height);
                println!("│ Width (W)       │ {:<30} │", h.width);
                println!("│ FPS             │ {:<30} │", h.fps);
                println!("└─────────────────┴──────────────────────────────┘");
                println!("文件: {}", path.display());
                }
            }
            "verify" => {
                let path = args
                    .next()
                    .map(PathBuf::from)
                    .ok_or("用法: vates verify <file.dct>")?;
                let report = Decoder::verify_file(&path).map_err(|e: DctError| e.to_string())?;
                println!("{}", report);
                println!("(OK) {}", path.display());
            }
            "encode" => {
                let out = args.next().map(PathBuf::from).unwrap_or_else(|| PathBuf::from("out.dct"));
                let w = 512u32;
                let h = 512u32;
                let c = 3u8;
                let data = synthetic_chw(c as usize, h as usize, w as usize);
                Encoder::encode_file(data.view(), 1, c as u32, h, w, 0, 24.0, &out, 3, None)?;
                println!("wrote {}", out.display());
            }
            "decode" => {
                let path = args
                    .next()
                    .map(PathBuf::from)
                    .expect("usage: decode <file.dct>");
                let (hdr, _) = Decoder::decode_file(&path)?;
                println!(
                    "B={} C={} H={} W={} mode={} reserved={} fps={}",
                    hdr.batch,
                    hdr.channels,
                    hdr.height,
                    hdr.width,
                    hdr.mode,
                    hdr.reserved,
                    hdr.fps
                );
            }
            "help" | "-h" | "--help" => print_help(),
            _ => {
                print_help();
                return Err(format!("未知命令: {cmd}").into());
            }
        }
        Ok(())
    };

    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}
