"""SCM Workbench — a local web console for silhouette-card-maker + scm-extras."""
from scm_workbench._version import __version__

# The app bundle's entry point (briefcase does `from scm_workbench import main`):
# the packaged launcher, which then hands over to the regular server.
from scm_workbench.launcher import main
