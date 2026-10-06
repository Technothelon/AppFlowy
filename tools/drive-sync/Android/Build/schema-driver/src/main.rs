// SPDX-License-Identifier: AGPL-3.0-only
fn main() {
 let root = std::path::PathBuf::from(std::env::args().nth(1).unwrap());
 let names = ["dart-ffi","flowy-ai","flowy-database2","flowy-date","flowy-document","flowy-error","flowy-folder","flowy-notification","flowy-search","flowy-storage","flowy-user"];
 for name in names { std::env::set_current_dir(root.join(name)).unwrap(); flowy_codegen::protobuf_file::dart_gen(name); }
 for name in names { std::env::set_current_dir(root.join(name)).unwrap(); flowy_codegen::dart_event::r#gen(name); }
}
