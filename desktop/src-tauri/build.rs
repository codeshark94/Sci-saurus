fn main() {
    println!(
        "cargo:rustc-env=SCIWHALE_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
    tauri_build::build();
}
