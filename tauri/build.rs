fn main() {
    // Build the app with its custom commands' permissions generated up front
    // (the `allow-wb-restart` and `allow-wb-rpc` app-command permissions,
    // referenced by name in capabilities/default.json).
    tauri_build::try_build(tauri_build::Attributes::default().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "wb_restart",
            "wb_rpc",
            "wb_pick_repo_directory",
            "wb_decklist_import",
            "wb_save_artifact",
        ]),
    ))
    .expect("failed to run the tauri build script");
    //
    // The exe's icon and Windows version metadata need no extra step:
    // tauri-build embeds both from tauri.conf.json (the bundle icon list and
    // the "version" that scripts/inject_version.py pins from the release
    // tag), so the exe's File Properties track the app's own version
    // reporting by construction.
}
