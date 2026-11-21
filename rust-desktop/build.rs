fn main() {
    let manifest =
        tauri_build::AppManifest::new().commands(&["snapshot", "select", "confirm", "retry"]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest))
        .expect("build desktop resources");
}
