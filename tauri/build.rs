fn main() {
    // Build the app with its custom commands' permissions generated up front
    // (the "command" namespace — e.g. command:allow-wb-restart — which the
    // capability in capabilities/default.json references).
    tauri_build::try_build(
        tauri_build::Attributes::default().app_manifest(
            tauri_build::AppManifest::new().commands(&["wb_restart"]),
        ),
    )
    .expect("failed to run the tauri build script");
}
