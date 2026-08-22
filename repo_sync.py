#!/usr/bin/env python3
"""Dev shim — the repo-sync CLI lives in scm_workbench/repo_sync.py.

    python repo_sync.py update --repo scm
"""
from scm_workbench.repo_sync import main

if __name__ == "__main__":
    main()
