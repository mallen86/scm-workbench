#!/bin/sh
# Local equivalent of CI's "briefcase build <platform> app" — adds the
# PIP_FIND_LINKS export the workflow does (see .github/workflows/package.yml),
# so a from-scratch build resolves the vendored proxy_tools wheel and
# completes, including the app-icon install.
#
#   Usage: scripts/build.sh [macos|windows] [app]
#   (no args = an error from briefcase tells you what it wanted)
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/briefcase ]; then
    echo "briefcase is not installed in .venv - set it up first (CONTRIBUTING.md, 'Packaging the app')." >&2
    exit 1
fi
# The build must run from the Python 3.13 venv the repo pins (pyproject.toml):
# that interpreter is what pip resolves the bundled wheels for.
if ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info[:2] != (3, 13))'; then
    echo ".venv is not Python 3.13 - recreate it (uv venv && uv sync), see CONTRIBUTING.md." >&2
    exit 1
fi
export PIP_FIND_LINKS="file://$PWD/ciwheels"
exec .venv/bin/briefcase build "$@"
