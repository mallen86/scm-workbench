"""SCM Workbench — a local web console for silhouette-card-maker + scm-extras.

The *packaged* app is the Tauri shell in ``tauri/``: one native webview that
spawns the bundled runtime running ``scm_workbench.server`` as a reaped child.
``python -m scm_workbench`` (below) is the source-checkout equivalent — it runs
that same server without a window, for development.
"""
from scm_workbench._version import __version__

# ``python -m scm_workbench`` entry point (dev): set up the data area and run
# the server. The packaged window is the Tauri binary, not this.
from scm_workbench.launcher import main
