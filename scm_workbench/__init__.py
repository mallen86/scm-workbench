"""SCM Workbench — a local web console for silhouette-card-maker + scm-extras."""
__version__ = "1.1.0"

# The app bundle's entry point (briefcase does `from scm_workbench import main`):
# the packaged launcher, which then hands over to the regular server.
from scm_workbench.launcher import main
