"""``python -m scm_workbench`` — the source-checkout entry point: run the UI
server (and bootstrap the managed repo copies) from a plain terminal. The
packaged app's window is the Tauri shell in ``tauri/``, not this; it simply
spawns the bundled runtime running this same server module.
"""
import sys

if __name__ == "__main__":
    # The launcher performs dev/bootstrap setup and writes human-readable
    # progress to stdout.  Native IPC reserves stdout for JSON frames, so
    # bypass that setup hook for the explicit child-protocol entry point.
    if "--ipc" in sys.argv[1:]:
        from scm_workbench import server
        server.main()
    else:
        from scm_workbench import main
        main()
