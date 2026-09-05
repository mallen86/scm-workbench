#!/usr/bin/env python3
"""
SCM Workbench — a local web UI for silhouette-card-maker and scm-extras.

Zero dependencies beyond the Python standard library. Run:

    python -m scm_workbench.server             # starts on http://127.0.0.1:8037
    python -m scm_workbench.server --port 9000  # different port
    python -m scm_workbench.server --no-browser  # don't auto-open a browser tab

The UI shells out to the Python scripts in the two sister repos (auto-detected
next to this folder, overridable in Settings) and streams their output live in
a job console. Every exposed CLI option is driven from a single manifest, so
the on-screen command preview always matches the command actually run.

Binds to 127.0.0.1 only — this is local tooling, not a network service.
Requires Python 3.10+ (3.12+ recommended to match silhouette-card-maker).
"""

import argparse
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import queue
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit
from typing import Any, Dict, List, Optional, Tuple

from scm_workbench import repo_sync, updater

# The one version constant the whole app reports (About-card line, banner,
# and the updater's notion of "what am I running"). It is pinned per build
# by scripts/inject_version.py from the release tag, so a build from v0.1.1
# says 0.1.1 everywhere and can never offer to install itself.
from scm_workbench._version import __version__

SERVER_VERSION = __version__
DEFAULT_PORT = 8037
# True only while this process owns the native child JSON-lines transport.
# stdout is reserved for protocol frames in that mode.
_IPC_MODE = False

# Native job responses and SSE frames share these conservative wire limits.
# Log files remain complete on disk; only transmitted lines are clipped.
JOB_LINE_MAX_BYTES = 64 * 1024
JOB_LOG_MAX_LINES = 4096
SSE_QUEUE_SIZE = 128


class _WakeQueue(queue.Queue):
    """Queue compatible with legacy producers but never blocks their pump."""
    def put(self, item, block=True, timeout=None):  # noqa: D401
        return super().put(item, block=False)

    def put_nowait(self, item):
        return super().put(item, block=False)


IPC_POLL_MAX_BYTES = 6 * 1024 * 1024  # safely below the 8 MiB frame ceiling


def _diag(message: str = "", *, error: bool = False) -> None:
    """Write human diagnostics without contaminating IPC stdout."""
    print(message, file=sys.stderr if (_IPC_MODE or error) else sys.stdout)

# The package lives one level down from the repo root in a dev checkout, and
# next to a `ui/` folder inside an app bundle; accept either layout.
_HERE = Path(__file__).resolve().parent

# When the app runs from a bundle (packaged with Briefcase/py2app), the
# launcher points SCM_WORKBENCH_DATA at a writable per-user area, so settings,
# job history, logs, and the managed repo copies survive app updates. In a dev
# checkout (env var unset) everything stays at the repo root, as before.
_env_data = os.environ.get("SCM_WORKBENCH_DATA")
DATA_DIR = Path(_env_data).expanduser().resolve() if _env_data else _HERE.parent / "data"
WB_ROOT = _HERE.parent

UI_DIR = next((c for c in (_HERE / "ui", _HERE.parent / "ui") if (c / "index.html").is_file()),
              _HERE.parent / "ui")
SETTINGS_FILE = DATA_DIR / "settings.json"
JOBS_FILE = DATA_DIR / "jobs.json"
LOGS_DIR = DATA_DIR / "logs"
PER_SIZE_OFFSETS_FILE = DATA_DIR / "offsets_by_size.json"
UPDATE_STATE_FILE = DATA_DIR / "update-state.json"
UPDATE_CHECK_INTERVAL = 86400          # re-check for a newer release at most once a day

# ============================================================================
# Repo detection & plain-JSON readers (no imports from the base repos)
# ============================================================================

def _try_read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _sibling(name: str, marker: str) -> Optional[Path]:
    base = Path(__file__).resolve().parent
    # walk up: the package dir, the repo root, and the folder holding the repo
    # (dev checkouts keep the sister repos side by side with the Workbench root)
    for _ in range(3):
        p = base / name
        if (p / marker).is_file():
            return p
        base = base.parent
    return None


def find_scm_repo() -> Optional[Path]:
    return _sibling("silhouette-card-maker", "create_pdf.py")


def find_extras_repo() -> Optional[Path]:
    return _sibling("scm-extras", "generate.py")


def read_scm_info(scm: Optional[Path], extras: Optional[Path]) -> dict:
    """Display info for the SCM repo, read from plain JSON files only."""
    info = {
        "found": bool(scm and scm.is_dir()),
        "path": str(scm) if scm else None,
        "version": None,
        "ppi": 300,
        "card_radius": "3mm",
        "card_sizes": [],
        "paper_sizes": [],
        "layouts": {},
        "specialty": [],
        "templates": {"dxf": [], "borderless_dxf": [], "studio3": [], "borderless_studio3": []},
        "calibration": [],
        "saved_offset": None,
        "decklists": [],
        "output_pdfs": [],
    }
    if not info["found"]:
        return info

    try:
        m = re.search(r'version\s*=\s*"([^"]+)"', (scm / "pyproject.toml").read_text(encoding="utf-8"))
        if m:
            info["version"] = m.group(1)
    except Exception:
        pass

    layouts = _try_read_json(scm / "assets" / "layouts.json") or {}
    info["ppi"] = layouts.get("ppi", 300)
    info["card_radius"] = layouts.get("defaults", {}).get("card_radius", "3mm")

    extra: dict = {}
    if extras:
        extra = _try_read_json(extras / "assets" / "layouts_extra.json") or {}

    for name, d in (layouts.get("card_sizes") or {}).items():
        info["card_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "radius": d.get("radius"), "aliases": d.get("aliases") or [], "source": "core",
        })
    for name, d in (extra.get("card_sizes") or {}).items():
        info["card_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "radius": d.get("radius"), "aliases": d.get("aliases") or [], "source": "extras",
        })
    for name, d in (layouts.get("paper_sizes") or {}).items():
        info["paper_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "aliases": d.get("aliases") or [], "source": "core",
        })
    for name, d in (extra.get("paper_sizes") or {}).items():
        info["paper_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "aliases": d.get("aliases") or [], "source": "extras",
        })

    merged: Dict[str, Any] = {}
    for paper, cards in (layouts.get("layouts") or {}).items():
        for card, variants in cards.items():
            for variant, defn in variants.items():
                merged.setdefault(paper, {}).setdefault(card, {})[variant] = defn
    for paper, cards in (extra.get("layouts") or {}).items():
        for card, variants in cards.items():
            for variant, defn in variants.items():
                merged.setdefault(paper, {}).setdefault(card, {})[variant] = defn
    info["layouts"] = merged

    for name, d in (layouts.get("specialty_layouts") or {}).items():
        cs = d.get("card_size") or {}
        info["specialty"].append({
            "name": name,
            "paper": (d.get("paper_size") or {}).get("name"),
            "width": cs.get("width"), "height": cs.get("height"),
            "rows": d.get("num_rows"), "cols": d.get("num_cols"),
        })

    ct = scm / "cutting_templates"
    info["templates"] = {
        "dxf": _sorted_dir(ct / "dxf"),
        "borderless_dxf": _sorted_dir(ct / "borderless" / "dxf"),
        "studio3": _glob_dir(ct, "*.studio3"),
        "borderless_studio3": _glob_dir(ct / "borderless", "*.studio3"),
    }

    cal = scm / "calibration"
    if cal.is_dir():
        info["calibration"] = [
            {"name": p.stem.replace("-calibration", ""), "path": str(p), "size": p.stat().st_size}
            for p in sorted(cal.glob("*.pdf"))
        ]

    offset = _try_read_json(scm / "data" / "offset_data.json")
    if offset:
        info["saved_offset"] = {
            "x": offset.get("x_offset", 0),
            "y": offset.get("y_offset", 0),
            "angle": offset.get("angle_offset", 0),
        }

    dl = scm / "game" / "decklist"
    if dl.is_dir():
        placeholders = {"README.md", "EMPTY.md"}
        info["decklists"] = [
            {"name": p.name, "size": p.stat().st_size}
            for p in sorted(dl.iterdir()) if p.is_file() and p.name not in placeholders
        ]
    outdir = scm / "game" / "output"
    if outdir.is_dir():
        info["output_pdfs"] = [p.name for p in sorted(outdir.glob("*.pdf"))]
    return info


def _sorted_dir(d: Path) -> List[str]:
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file())


def _glob_dir(d: Path, pattern: str) -> List[str]:
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob(pattern))


def read_extras_info(extras: Optional[Path]) -> dict:
    info = {
        "found": bool(extras and extras.is_dir()),
        "path": str(extras) if extras else None,
        "card_sizes": [],
        "layouts": {},
        "templates": {"dxf": [], "borderless_dxf": [], "studio3": [], "borderless_studio3": []},
    }
    if not info["found"]:
        return info
    extra = _try_read_json(extras / "assets" / "layouts_extra.json") or {}
    for name, d in (extra.get("card_sizes") or {}).items():
        info["card_sizes"].append({
            "name": name, "width": d.get("width"), "height": d.get("height"),
            "radius": d.get("radius"), "aliases": d.get("aliases") or [],
        })
    info["layouts"] = extra.get("layouts", {})
    ct = extras / "cutting_templates"
    info["templates"] = {
        "dxf": _sorted_dir(ct / "dxf"),
        "borderless_dxf": _sorted_dir(ct / "borderless" / "dxf"),
        "studio3": _glob_dir(ct, "*.studio3"),
        "borderless_studio3": _glob_dir(ct / "borderless", "*.studio3"),
    }
    return info


# ============================================================================
# Game plugins (mirrors silhouette-card-maker/plugins/*/fetch.py)
# ============================================================================

PLUGINS: Dict[str, dict] = {
    "mtg": {
        "title": "Magic: The Gathering",
        "formats": [
            ("archidekt", "Archidekt"), ("cubecobra_csv", "CubeCobra CSV"),
            ("deckstats", "Deckstats"), ("moxfield", "Moxfield"),
            ("mpcfill_xml", "MPCFill XML"), ("mtga", "MTG Arena"),
            ("mtgo", "MTGO"), ("scryfall_json", "Scryfall JSON"),
            ("simple", "Simple (name list)"), ("url", "URL"),
        ],
    },
    "yugioh": {"title": "Yu-Gi-Oh!", "formats": [("ydke", "YDKE"), ("ydk", "YDK")]},
    "pokemon": {"title": "Pokémon", "formats": [("limitless", "Limitless TCG")]},
    "altered": {"title": "Altered", "formats": [("ajordat", "Ajordat")]},
    "arkham_horror_lcg": {
        "title": "Arkham Horror LCG",
        "formats": [("arkhamdb_json", "ArkhamDB JSON"), ("arkhamdb_url", "ArkhamDB URL")],
    },
    "ashes_reborn": {
        "title": "Ashes: Reborn",
        "formats": [("ashes_share_url", "Ashes share URL"), ("ashesdb_share_url", "AshesDB share URL")],
    },
    "bushiroad": {"title": "Bushiroad", "formats": [("bushiroad_url", "Bushiroad URL")]},
    "digimon": {
        "title": "Digimon Card Game",
        "formats": [
            ("digimoncardapp", "Digimon Card App"), ("digimoncarddev", "Digimon Card Dev"),
            ("digimoncardio", "DigimonCard.io"), ("digimonmeta", "Digimon Meta"),
            ("tts", "Tabletop Simulator"), ("untap", "Untap"),
        ],
    },
    "echoes_of_astra": {"title": "Echoes of Astra", "formats": [("astrabuilder_url", "Astra Builder URL")]},
    "elestrals": {"title": "Elestrals", "formats": [("elestrals", "Elestrals")]},
    "final_fantasy": {
        "title": "Final Fantasy TCG",
        "formats": [("octgn_xml", "OctGN XML"), ("tts", "Tabletop Simulator"), ("untap", "Untap")],
    },
    "flesh_and_blood": {"title": "Flesh & Blood", "formats": [("fabrary", "Fabrary")]},
    "grand_archive": {"title": "Grand Archive", "formats": [("omnideck", "OmniDeck")]},
    "gundam": {
        "title": "Gundam Card Game",
        "formats": [
            ("deckplanet", "Deckplanet"), ("egman", "EGM Events"),
            ("exburst", "Exburst"), ("limitless", "Limitless TCG"),
        ],
    },
    "keyforge": {
        "title": "KeyForge",
        "formats": [
            ("archon_arcana", "Archon Arcana"), ("master_vault_url", "Master Vault URL"),
            ("decks_of_keyforge_url", "Decks of Keyforge URL"),
        ],
    },
    "lorcana": {"title": "Disney Lorcana", "formats": [("dreamborn", "Dreamborn")]},
    "lotr_lcg": {
        "title": "Lord of the Rings LCG",
        "formats": [
            ("ringsdb_url", "RingsDB URL"), ("ringsdb_fellowship_url", "RingsDB Fellowship URL"),
            ("ringsdb_scenario_url", "RingsDB Scenario URL"), ("hallofbeorn_url", "Hall of Beorn URL"),
        ],
    },
    "netrunner": {
        "title": "Netrunner",
        "formats": [
            ("bbcode", "BBCode"), ("jinteki", "Jinteki"), ("markdown", "Markdown"),
            ("plain_text", "Plain text"), ("text", "Text"),
        ],
    },
    "one_piece": {
        "title": "One Piece Card Game",
        "formats": [("egman", "EGM Events"), ("optcgsim", "OPTCG Simulator")],
    },
    "riftbound": {
        "title": "Riftbound",
        "formats": [
            ("piltover_archive", "Piltover Archive"), ("pixelborn", "Pixelborn"),
            ("tts", "Tabletop Simulator"),
        ],
    },
    "sorcery_contested_realm": {
        "title": "Sorcery: Contested Realm",
        "formats": [("curiosa_url", "Curiosa URL")],
    },
    "star_wars_unlimited": {
        "title": "Star Wars Unlimited",
        "formats": [("melee", "Melee"), ("picklist", "Picklist"), ("swudb_json", "SWUDB JSON")],
    },
}

MTG_LANGS = ["en", "sp", "fr", "de", "it", "pt", "jp", "kr", "ru", "cs", "ct", "ag", "ph"]

# ============================================================================
# Job manifest — single source of truth for every exposed option.
# Served to the browser (forms are rendered from it) and consumed by the
# argv builders below, so the command preview always matches the command run.
# ============================================================================

def _opt(key, label, type_, **kw) -> dict:
    d = {"key": key, "label": label, "type": type_}
    d.update(kw)
    return d


def build_manifest(info: dict) -> dict:
    """Assemble the job manifest. `info` = output of get_info()."""
    scm, extras = info["scm"], info["extras"]

    card_choices = [["", "— pick a card size —"]]
    for c in scm["card_sizes"]:
        tag = "   ✦ extras" if c["source"] == "extras" else ""
        card_choices.append([c["name"], f"{c['name']} — {c.get('width') or '?'} × {c.get('height') or '?'}{tag}"])
    paper_choices = [["", "— pick a paper size —"]]
    for p in scm["paper_sizes"]:
        paper_choices.append([p["name"], f"{p['name']} — {p.get('width') or '?'} × {p.get('height') or '?'}"])
    specialty_choices = [["", "None"]] + [
        [s["name"], f"{s['name']} ({s['paper']})"] for s in scm["specialty"]
    ]

    def fetch_groups(slug: str) -> List[dict]:
        groups = [
            {
                "title": "Decklist",
                "options": [
                    _opt("deck_source", "Source", "segment",
                         choices=[["file", "Existing file"], ["paste", "Paste text"], ["url", "URL"]], default="file"),
                    _opt("deck_file", "Decklist file", "select",
                         choices=[["", "— pick a file —"]] + [[d["name"], d["name"]] for d in scm["decklists"]],
                         default=""),
                    _opt("deck_name", "Save pasted decklist as", "text", placeholder="my_deck.txt", default=""),
                    _opt("deck_text", "Decklist text", "textarea", default=""),
                    _opt("deck_url", "URL", "text", placeholder="https://archidekt.io/deck/…", default="",
                         help="For URL-based formats (MTG “url”, any *_url) the decklist is the URL itself."),
                ],
            },
            {
                "title": "Format",
                "options": [
                    _opt("format", "Decklist format", "select",
                         choices=[["", "— pick a format —"]] + [list(f) for f in PLUGINS[slug]["formats"]],
                         default=""),
                ],
            },
        ]
        if slug == "mtg":
            groups.append({
                "title": "MTG card preferences",
                "collapsible": True,
                "options": [
                    _opt("prefer_set", "Prefer sets", "chips", placeholder="e.g. ONE, M25", default=[]),
                    _opt("ignore_set", "Exclude sets", "chips", default=[]),
                    _opt("prefer_lang", "Preferred languages (printed code)", "choice_chips",
                         choices=[[l, l.upper()] for l in MTG_LANGS], default=[]),
                    _opt("prefer_older_sets", "Prefer older sets", "toggle", default=False),
                    _opt("prefer_showcase", "Prefer showcase art", "toggle", default=False),
                    _opt("prefer_extra_art", "Prefer full / borderless / extended art", "toggle", default=False),
                    _opt("prefer_ub", "Prefer Universe Beyond", "toggle", default=False),
                    _opt("ignore_ub", "Exclude Universe Beyond", "toggle", default=False),
                    _opt("tokens", "Also fetch related tokens", "toggle", default=False),
                    _opt("ignore_set_and_collector_number", "Ignore set & collector numbers", "toggle", default=False),
                ],
            })
        return groups

    kinds: Dict[str, dict] = {}

    # ------------------------------------------------------------------ PDF
    kinds["create_pdf"] = {
        "title": "Create PDF", "page": "pdf", "needs": ["scm"], "cwd": "scm",
        # the simple-mode layout: rows of the flat section — the two
        # dropdowns alone up top, the four toggles together below
        "simple_rows": [
            ["card_size", "paper_size"],
            ["borderless", "load_offset", "only_fronts", "mpcfill_crop"],
        ],
        "description": "Lays out card images into a print-ready PDF with registration marks that match the cutting templates.",
        "groups": [
            {
                "title": "Sources & output",
                "options": [
                    _opt("front_dir", "Front images folder", "path", default="game/front", width="half",
                         help="Directory containing the card front images."),
                    _opt("back_dir", "Card back folder", "path", default="game/back", width="half",
                         help="Directory with one or more card back images."),
                    _opt("double_sided_dir", "Double-sided folder", "path", default="game/double_sided", width="half",
                         help="Cards that have different front and back art."),
                    _opt("output_path", "Output PDF", "path", default="game/output/game.pdf", width="full"),
                    _opt("output_images", "Output images instead of a PDF", "toggle", default=False, width="third"),
                    _opt("only_fronts", "Front pages only", "toggle", default=False, width="third", simple=True),
                ],
            },
            {
                "title": "Card, paper & registration",
                "options": [
                    _opt("card_size", "Card size", "select", choices=card_choices, default="standard", width="third", simple=True),
                    _opt("paper_size", "Paper size", "select", choices=paper_choices, default="letter", width="third", simple=True),
                    _opt("registration", "Registration marks", "segment",
                         choices=[["3", "3 marks"], ["4", "4 marks"]], default="3", width="third"),
                    _opt("specialty", "Specialty layout", "select", choices=specialty_choices, default="", width="third",
                         help="Overrides card size, paper size, and registration."),
                    _opt("registration_orientation", "Registration orientation", "select",
                         choices=[["", "Auto (follow layout)"], ["portrait", "Portrait"], ["landscape", "Landscape"]],
                         default="", width="third"),
                    _opt("borderless", "Borderless (tighter inset)", "toggle", default=False, width="third", simple=True,
                         help="Fits more cards per page by using a smaller inset."),
                ],
            },
            {
                "title": "Quality",
                "options": [
                    _opt("ppi", "Resolution (PPI)", "range", default=1200, min=150, max=1200, step=10, width="third"),
                    _opt("quality", "Compression quality", "range", default=100, min=0, max=100, step=1, width="third"),
                    _opt("load_offset", "Apply saved offset", "toggle", default=False, width="third", simple=True,
                         help="Applies the saved X / Y / angle printer offset — the matching per-paper-size row when one is saved, else the global value."),
                ],
            },
            {
                "title": "Fit & edge finishing",
                "collapsible": True,
                "options": [
                    _opt("fit", "Fit front images", "segment",
                         choices=[["stretch", "Stretch"], ["crop", "Center crop"]], default="stretch", width="third",
                         help="Stretch allows distortion; crop preserves aspect ratio."),
                    _opt("fit_backs", "Fit back images", "segment",
                         choices=[["", "Auto (like fronts)"], ["stretch", "Stretch"], ["crop", "Center crop"]],
                         default="", width="third"),
                    _opt("mpcfill_crop", "MPCFill Crop", "toggle", default=False, width="third", simple=True, simple_only=True,
                         help="Applies a 3mm crop to the front images to fix MPCFill's padding — the art it fetches ships with its own print-bleed margin. A value typed in “Crop edges (fronts)” wins over this toggle."),
                    _opt("crop", "Crop edges (fronts)", "text", placeholder="3mm · 0.125in", width="third"),
                    _opt("crop_backs", "Crop edges (backs)", "text", placeholder="3mm · 0.125in", width="third"),
                    _opt("extend_edges", "Extend edges (fronts)", "text", placeholder="3mm", width="third"),
                    _opt("extend_edges_backs", "Extend edges (backs)", "text", placeholder="3mm", width="third"),
                    _opt("extend_corners", "Extend rounded corners (fronts)", "text", placeholder="3mm", width="third"),
                    _opt("extend_corners_backs", "Extend rounded corners (backs)", "text", placeholder="3mm", width="third"),
                    _opt("extend_bleed", "Extend outer bleed (front pages)", "text", placeholder="3mm", width="third"),
                    _opt("extend_bleed_backs", "Extend outer bleed (back pages)", "text", placeholder="3mm", width="third"),
                ],
            },
            {
                "title": "Advanced",
                "collapsible": True,
                "options": [
                    _opt("skip", "Skip card indexes", "chips", int=True, placeholder="0, 4", width="half",
                         help="0-based indexes of cards to skip (works around a bad registration)."),
                    _opt("label", "Custom page label", "text", width="half"),
                    _opt("show_outline", "Show white cut outline", "toggle", default=False, width="half"),
                ],
            },
        ],
    }

    # --------------------------------------------------------------- Offset
    kinds["offset_pdf"] = {
        "title": "Offset PDF", "page": "offset", "needs": ["scm"], "cwd": "scm",
        "description": "Shifts a printed PDF by an X/Y offset and rotation angle, then re-assembles it. Used to correct printer misalignment.",
        "groups": [
            {
                "title": "Source PDF",
                "options": [
                    _opt("pdf_path", "Input PDF", "select",
                         choices=[["", "— pick a PDF —"]] + [[p, p] for p in scm["output_pdfs"]]
                               + [["game/output/game.pdf", "game/output/game.pdf (default)"]],
                         default="game/output/game.pdf", width="half"),
                    _opt("output_pdf_path", "Output PDF (blank = auto)", "path", width="half",
                         help="Defaults to <input>_offset.pdf next to the input file."),
                ],
            },
            {
                "title": "Offset values",
                "options": [
                    _opt("paper_size", "Paper size (per-size row)", "select",
                         choices=[["", "— global only —"]] + [
                             [p["name"], f"{p['name']} — {p.get('width') or '?'} × {p.get('height') or '?'}"]
                             for p in scm["paper_sizes"]],
                         default="", width="third",
                         help="Which per-size row to work with: it prefills the fields below and is what “Save” records into. Blank = the single global offset."),
                    _opt("x_offset", "X offset (px, right +)", "number", default="", width="quarter"),
                    _opt("y_offset", "Y offset (px, up +)", "number", default="", width="quarter"),
                    _opt("angle", "Angle (deg, clockwise +)", "number", step=0.1, default="", width="quarter"),
                    _opt("ppi", "PPI", "range", default=1200, min=150, max=1200, step=10, width="half"),
                    _opt("save", "Save these as the new offset", "toggle", default=False, width="half"),
                    _opt("use_saved", "Prefill fields from the saved offset", "toggle", default=True, width="half"),
                ],
            },
        ],
    }

    # ------------------------------------------------------------ Calibration
    kinds["calibration"] = {
        "title": "Calibration sheets", "page": "offset", "needs": ["scm"], "cwd": "scm",
        "description": "Generates a two-page alignment sheet for every paper size. Print double-sided (long-edge flip), compare the dot grids, and measure misalignment.",
        "groups": [],
    }

    # ------------------------------------------------------------- Templates
    kinds["dxf_single"] = {
        "title": "Generate a cutting template (DXF)", "page": "templates", "needs": ["scm"], "cwd": "scm",
        "description": "Creates one DXF cutting template for a card-size × paper-size combination.",
        "groups": [
            {
                "title": "Card size",
                "options": [
                    _opt("card_mode", "Use", "segment", choices=[["named", "A named size"], ["custom", "Custom dimensions"]], default="named", width="half"),
                    _opt("card_size", "Named card size", "select",
                         choices=[["", "— pick —"]] + [[c["name"], f"{c['name']} — {c.get('width') or '?'} × {c.get('height') or '?'}"] for c in scm["card_sizes"]],
                         default="standard", width="half"),
                    _opt("card_width", "Custom width", "text", placeholder="63mm · 2.5in", width="quarter"),
                    _opt("card_height", "Custom height", "text", placeholder="88mm · 3.5in", width="quarter"),
                    _opt("card_radius", "Custom corner radius", "text", placeholder="3mm", width="quarter"),
                    _opt("card_name", "Card label (for filename)", "text", width="quarter",
                         help="Optional; only used for the output file name."),
                ],
            },
            {
                "title": "Paper size",
                "options": [
                    _opt("paper_mode", "Use", "segment", choices=[["named", "A named size"], ["custom", "Custom dimensions"]], default="named", width="half"),
                    _opt("paper_size", "Named paper size", "select",
                         choices=[["", "— pick —"]] + [[p["name"], f"{p['name']} — {p.get('width') or '?'} × {p.get('height') or '?'}"] for p in scm["paper_sizes"]],
                         default="letter", width="half"),
                    _opt("paper_width", "Custom width (shorter side)", "text", placeholder="8.5in · 210mm", width="quarter"),
                    _opt("paper_height", "Custom height (longer side)", "text", placeholder="11in · 297mm", width="quarter"),
                    _opt("paper_name", "Paper label (for filename)", "text", width="quarter",
                         help="Optional; only used for the output file name."),
                ],
            },
            {
                "title": "Layout & output",
                "options": [
                    _opt("variant", "Variant", "segment", choices=[["default", "Default"], ["borderless", "Borderless"]], default="default", width="third"),
                    _opt("orientation", "Orientation", "segment",
                         choices=[["optimize", "Optimize"], ["landscape", "Landscape"], ["portrait", "Portrait"]],
                         default="optimize", width="third"),
                    _opt("output_path", "Output file (blank = auto)", "path", width="full",
                         help="Defaults to cutting_templates/dxf/<paper>-<card>-v1.dxf (…/borderless/dxf/ for borderless)."),
                    _opt("save", "Save new size / layout to layouts.json", "toggle", default=True, width="half"),
                ],
            },
        ],
    }

    kinds["dxf_batch"] = {
        "title": "Batch generate DXF templates", "page": "templates", "needs": ["scm"], "cwd": "scm",
        "description": "Generates DXF templates for the standard paper × card size matrix in the repo.",
        "groups": [
            {
                "title": "Mode",
                "options": [
                    _opt("mode", "Mode", "segment",
                         choices=[["missing", "Missing only"], ["all", "Regenerate all"], ["optimize", "Re-optimize orientations"]],
                         default="missing"),
                ],
            },
        ],
    }

    kinds["dxf_list"] = {
        "title": "List available sizes", "page": "utilities", "needs": ["scm"], "cwd": "scm",
        "description": "Prints every card and paper size known to the repo (including extras).",
        "groups": [],
    }

    kinds["clean_up"] = {
        "title": "Clear card image folders", "page": "utilities", "needs": ["scm"], "cwd": "scm",
        "description": "Deletes every image in game/front/ and game/double_sided/ so you can start a new game fresh. The folder README placeholders are kept, and the card back folder is left untouched.",
        "groups": [],
    }

    # ------------------------------------------------------------- Repo copies
    kinds["repo_update"] = {
        "title": "Update a managed repo", "page": "settings", "needs": [], "cwd": "wb",
        "description": "Moves a Workbench-managed copy of a sister repo to the chosen ref. Forward moves fetch only the changed files; rollbacks and large jumps take a full snapshot. Your images, decklists, and local edits are preserved.",
        "groups": [
            {
                "title": "Target",
                "options": [
                    _opt("repo", "Repo", "segment",
                         choices=[["scm", "silhouette-card-maker"], ["extras", "scm-extras"]],
                         default="scm", width="half"),
                    _opt("force_full", "Force full snapshot", "toggle", default=False, width="half",
                         help="Skip the changed-files diff and swap the whole tree (use if a diff misbehaves)."),
                ],
            },
        ],
    }
    kinds["repo_init"] = {
        "title": "Download a managed repo copy", "page": "settings", "needs": [], "cwd": "wb",
        "description": "Fetches a complete copy of a sister repo into the Workbench's own data area, so the app never needs a system Python or a hand-rolled clone.",
        "groups": [
            {
                "title": "Target",
                "options": [
                    _opt("repo", "Repo", "segment",
                         choices=[["scm", "silhouette-card-maker"], ["extras", "scm-extras"]],
                         default="scm", width="half"),
                ],
            },
        ],
    }

    # ---------------------------------------------------------------- Extras
    kinds["extras_generate"] = {
        "title": "Generate extras DXF templates", "page": "extras", "needs": ["extras"], "cwd": "extras",
        "description": "Generates the DXF cutting templates for the extra card sizes (MTG, Sorcery) into scm-extras/cutting_templates/. Finds Silhouette Card Maker automatically as a sister folder and wires SCM_EXTRA_LAYOUTS for you.",
        "groups": [
            {
                "title": "Mode",
                "options": [
                    _opt("mode", "Mode", "segment", choices=[["missing", "Missing only"], ["all", "Regenerate all"]], default="missing"),
                ],
            },
        ],
    }

    kinds["extras_tables"] = {
        "title": "Extras README tables", "page": "extras", "needs": ["extras"], "cwd": "extras",
        "description": "Renders the markdown size tables for the extra card sizes (paste into the README when they change).",
        "groups": [],
    }

    # --------------------------------------------------------------- Plugins
    for slug, meta in PLUGINS.items():
        kinds[f"fetch:{slug}"] = {
            "title": "Fetch Card Art",
            "game": meta["title"],
            # jobs (console tabs, recent jobs) run per game — keep them
            # distinguishable even though the button/heading title is generic
            "job_title": f"Fetch Card Art ({meta['title']})",
            "page": "fetch", "needs": ["scm"], "cwd": "scm", "slug": slug,
            "description": f"Downloads card images for {meta['title']} from a decklist into the game/ folders.",
            # Simple mode lays the form out as one flat row per group — the
            # standard 3-per-row rhythm the PDF page uses (create_pdf's
            # `simple_rows` does it in the manifest because the PDF groups are
            # static; here the groups are built per game, so the rows are added
            # below for the one game whose groups exceed the default shape).
            "groups": fetch_groups(slug),
            # Every game's fetch form: the decklist group (source + file/name/
            # text/URL), then the format group, then each preferences group in
            # its own row (MTG's has 3 toggle-per-row sub-rows inside it).
            "simple_rows": [[o["key"] for o in g["options"]] for g in fetch_groups(slug) if not g.get("collapsible")],
            # URL-based formats (consumed by the same rule the command builder
            # uses): the client auto-selects one of these when the source is URL.
            "url_formats": [f for f, _ in meta["formats"] if f == "url" or f.endswith("_url")],
            # XML-based formats: the client auto-selects one of these when the
            # picked existing decklist file is an .xml (e.g. MTG's MPCFill XML).
            "xml_formats": [f for f, _ in meta["formats"] if f.endswith("_xml")],
        }
        # MTG alone has a preferences group (7 toggles): give it the standard
        # 3-per-row flat layout — the group-level rows above become sub-rows
        # rendered inside the group's own row.
        if slug == "mtg":
            kinds[f"fetch:{slug}"]["groups"][2]["simple_rows"] = [
                ["prefer_set", "ignore_set", "prefer_lang"],
                ["prefer_older_sets", "prefer_showcase", "prefer_extra_art"],
                ["prefer_ub", "ignore_ub", "tokens"],
                ["ignore_set_and_collector_number"],
            ]

    return kinds


# ============================================================================
# Settings
# ============================================================================

DEFAULT_SETTINGS: Dict[str, Any] = {
    "scm_dir": "",
    "extras_dir": "",
    "python": "",
    "port": DEFAULT_PORT,
    "theme": "dark",
    "ui_mode": "simple",
    "auto_open_browser": True,
    "onboarded": False,
    "defaults": {
        "card_size": "standard",
        "paper_size": "letter",
        "ppi": 1200,
        "quality": 100,
    },
    "repos": {
        "scm": {"source": "latest-release", "pin": ""},
        "extras": {"source": "main", "pin": ""},
    },
}


_GIF_1PX = bytes.fromhex("474946383961010001000000000021ff0b4e65747363617065000000003b")


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _try_read_json(SETTINGS_FILE)
    if data:
        for k, v in data.items():
            if k in ("defaults", "repos") and isinstance(v, dict):
                s.setdefault(k, {})
                if isinstance(s.get(k), dict):
                    for k2, v2 in v.items():
                        if k2 in s[k] and isinstance(s[k][k2], dict) and isinstance(v2, dict):
                            s[k][k2].update(v2)
                        else:
                            s[k][k2] = v2
            else:
                s[k] = v
    return s


def save_settings(s: dict) -> None:
    # atomic: write to a sibling temp file and rename over the real one, so a
    # crash (or a second writer) can never leave a half-written settings.json
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# The server is threaded; two settings writers in flight would otherwise
# interleave their read-modify-write and one change would be lost.  Both the
# HTTP settings endpoint and native settings.set use this lock.
_SETTINGS_LOCK = threading.Lock()

SETTINGS_CHANGES_MAX_BYTES = 64 * 1024
_SETTINGS_PATH_FIELDS = frozenset(("scm_dir", "extras_dir", "python"))
_SETTINGS_FIELDS = frozenset((
    "scm_dir", "extras_dir", "python", "port", "theme", "ui_mode",
    "auto_open_browser", "onboarded", "defaults",
))
_DEFAULT_FIELDS = frozenset(("card_size", "paper_size", "ppi", "quality"))


def _setting_string(value: Any, *, name: str, max_bytes: int,
                    nonempty: bool = False) -> Optional[str]:
    """Validate a user-controlled UTF-8 string and return an error, if any."""
    if not isinstance(value, str):
        return f"{name} must be a string"
    if nonempty and not value:
        return f"{name} must be non-empty"
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return f"{name} must be valid UTF-8"
    if size > max_bytes:
        return f"{name} exceeds {max_bytes} UTF-8 bytes"
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        return f"{name} contains C0/DEL control characters"
    return None


def validate_settings_changes(changes: Any) -> List[str]:
    """Return bounded, application-level errors for a settings patch.

    This is deliberately shared by the HTTP and native transports.  The
    native method validates its exact ``{changes: ...}`` envelope separately;
    values and the resulting merge have one semantic implementation here.
    """
    if not isinstance(changes, dict):
        return ["changes must be an object"]
    if not changes:
        return ["changes must contain at least one setting"]
    try:
        encoded = json.dumps(changes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return ["changes must be valid JSON"]
    if len(encoded) > SETTINGS_CHANGES_MAX_BYTES:
        return ["changes exceed 64 KiB when encoded"]

    errors: List[str] = []
    for key in changes:
        if not isinstance(key, str) or key not in _SETTINGS_FIELDS:
            errors.append(f"unknown setting: {key}")
    for key, value in changes.items():
        if key in _SETTINGS_PATH_FIELDS:
            error = _setting_string(value, name=key, max_bytes=4096)
            if error:
                errors.append(error)
        elif key == "port":
            if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
                errors.append("port must be an integer from 1024 through 65535")
        elif key == "theme":
            if value not in ("dark", "light") or not isinstance(value, str):
                errors.append("theme must be dark or light")
        elif key == "ui_mode":
            if value not in ("simple", "advanced") or not isinstance(value, str):
                errors.append("ui_mode must be simple or advanced")
        elif key in ("auto_open_browser", "onboarded"):
            if not isinstance(value, bool):
                errors.append(f"{key} must be boolean")
        elif key == "defaults":
            if not isinstance(value, dict):
                errors.append("defaults must be an object")
                continue
            if not value:
                errors.append("defaults must contain at least one setting")
                continue
            for nested_key in value:
                if not isinstance(nested_key, str) or nested_key not in _DEFAULT_FIELDS:
                    errors.append(f"unknown default: {nested_key}")
            for nested_key, nested_value in value.items():
                label = f"defaults.{nested_key}"
                if nested_key in ("card_size", "paper_size"):
                    error = _setting_string(nested_value, name=label, max_bytes=128, nonempty=True)
                    if error:
                        errors.append(error)
                elif nested_key in ("ppi", "quality"):
                    if (isinstance(nested_value, bool) or
                            not isinstance(nested_value, (int, float)) or
                            (isinstance(nested_value, float) and not math.isfinite(nested_value))):
                        errors.append(f"{label} must be a finite number")
                    elif nested_key == "ppi" and not 0 <= nested_value <= 10000:
                        errors.append(f"{label} must be from 0 through 10000")
                    elif nested_key == "quality" and not 0 <= nested_value <= 100:
                        errors.append(f"{label} must be from 0 through 100")
    return errors


def update_settings(changes: Any) -> dict:
    """Atomically validate and apply a settings patch.

    Invalid patches are application results (the HTTP transport sends them as
    400, while native settings.set returns this same result in its ``result``
    field).  The lock covers validation, load, merge, and atomic commit so this
    helper is also safe against callers changing a patch concurrently.  A
    successful save invalidates both manifest and repo snapshots so path/default
    changes are visible immediately.  ``repos`` is intentionally not accepted
    here; /api/repos/save remains its separate locked writer.
    """
    with _SETTINGS_LOCK:
        errors = validate_settings_changes(changes)
        if errors:
            return {"ok": False, "errors": errors}
        settings = load_settings()
        for key, value in changes.items():
            if key == "defaults":
                settings.setdefault("defaults", {}).update(value)
            else:
                settings[key] = value
        try:
            encoded_size = len(json.dumps(
                settings, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            return {"ok": False, "errors": ["merged settings must be valid JSON"]}
        if encoded_size > SETTINGS_CHANGES_MAX_BYTES:
            return {"ok": False, "errors": ["merged settings exceed 64 KiB when encoded"]}
        save_settings(settings)
        invalidate_manifest_cache()
        _INFO_SNAP.clear()
        _REPOS_MTIME.clear()
    return {"ok": True, "settings": settings}

# ============================================================================
# App updates (see updater.py)
# ============================================================================

# one in-flight release check at a time (the daily daemon and a manual button
# press must not double-fire network calls)
_UPDATE_CHECK_IN_FLIGHT = False
_UPDATE_STATE_LOCK = threading.Lock()


def _own_bundle() -> str:
    """The .app bundle this server runs out of (None for a dev checkout).

    The install job needs the *path* of the bundle to swap, and it cannot
    be asked for by an environment variable: the worker the Tauri shell
    spawns inherits the shell's environment, which never carries one - and
    a missing var read as "not packaged" is exactly the failure that made
    a real install a silent no-op (the job downloaded, extracted, and ended
    with "no app folder to swap" while the app sat untouched in
    /Applications). The path is right here in sys.argv[0] instead:
    <App>.app/Contents/MacOS/<exe>. The old env knob stays honored for
    tests that set it explicitly."""
    if os.environ.get("SCM_WORKBENCH_BUNDLE"):
        return os.environ["SCM_WORKBENCH_BUNDLE"]
    if not os.environ.get("SCM_WORKBENCH_PACKAGED"):
        return None
    exe = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if exe:
        for parent in exe.parents:
            if parent.suffix == ".app":
                return str(parent)
    return None


def load_update_state() -> dict:
    st = _try_read_json(UPDATE_STATE_FILE)
    return st or {"status": "never", "current": SERVER_VERSION, "checked_at": None,
                  "latest": None, "asset": None, "reason": None, "release_url": None,
                  "published": None}


def save_update_state(st: dict) -> None:
    # atomic, like settings — a torn state file must never read as “checked”
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPDATE_STATE_FILE.with_name(UPDATE_STATE_FILE.name + ".tmp")
    with _UPDATE_STATE_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, UPDATE_STATE_FILE)


def run_update_check() -> dict:
    """One network check of the newest release of the Workbench repo.

    The state it produces (and persists) drives the Settings card: when it is
    fresh (< UPDATE_CHECK_INTERVAL) and says up-to-date, a check is a no-op.
    """
    global _UPDATE_CHECK_IN_FLIGHT
    st = {"status": "never", "current": SERVER_VERSION, "checked_at": None,
           "latest": None, "asset": None, "reason": None, "release_url": None,
           "published": None}
    if _UPDATE_CHECK_IN_FLIGHT:
        st = load_update_state()
        st["checking"] = True
        return st
    _UPDATE_CHECK_IN_FLIGHT = True
    try:
        try:
            rel = updater.latest_release()
        except updater.AuthRequiredError as e:
            st.update(status="auth-required", reason=str(e), checked_at=time.time())
        except updater.UpdateError as e:
            st.update(status="error", reason=str(e), checked_at=time.time())
        else:
            if rel.get("tag") and (
                updater.is_newer(rel["tag"], SERVER_VERSION)
                or rel["tag"].lstrip("v") == SERVER_VERSION
        ):
            # The second arm: the check found a release equal to the running
            # version - a check that ran while the release it saw was still
            # unpublished, or a manual re-check. The card treats this as
            # "update-available" (the user pressed a button and expects the
            # install flow), and the install's re-verification closes it out
            # cleanly instead of dead-ending on "Nothing to do".
                try:
                    asset = updater.pick_asset(rel)
                except updater.UpdateError as e:
                    st.update(status="error", checked_at=time.time(),
                              reason=f"{rel['tag']} is out, but: {e}")
                else:
                    st.update(status="update-available", latest=rel["tag"], asset=asset,
                              release_url=rel.get("url"), published=rel.get("published"),
                              checked_at=time.time())
            else:
                st.update(status="up-to-date", latest=rel.get("tag") or None,
                          checked_at=time.time())
    finally:
        _UPDATE_CHECK_IN_FLIGHT = False
    save_update_state(st)
    return st


def _update_daemon() -> None:
    """Check at server start, then once a day while the app is open."""
    time.sleep(5)  # let the window and its first paint land first
    while True:
        try:
            st = load_update_state()
            age = None if st.get("checked_at") is None else time.time() - float(st["checked_at"])
            if st.get("status") == "never" or age is None or age > UPDATE_CHECK_INTERVAL:
                out = run_update_check()
                tag = out.get("latest") or ""
                _diag(f"[updater] release check: {out.get('status')}" + (f" → {tag}" if tag else ""))
        except Exception as e:
            _diag(f"[updater] check failed: {e}")
        time.sleep(1800)


def start_update_job(requested_latest: str, force: bool) -> Tuple[Optional[dict], List[str]]:
    """The in-process install job (download → swap → relaunch → quit)."""
    st = load_update_state()
    if st.get("status") != "update-available" and not force:
        return None, ["No update is known to be available — press “Check for updates” first."]
    latest = st.get("latest") or requested_latest or "the newest release"
    job_id = uuid.uuid4().hex[:10]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    job: dict = {
        "id": job_id,
        "ts": time.time(),
        "kind": "update",
        "title": f"Update the app to {latest}",
        "cmd": f"workbench: self-update → {latest}",
        "args": {},
        "status": "running",
        "exit_code": None,
        "log_file": str(LOGS_DIR / f"{job_id}.log"),
        "log_lines": [],
        "first_seq": 0,
        "subs": [],
        "warnings": [],
        "started": time.time(),
        "ended": None,
        "duration": None,
        "proc": None,
        "progress": None,
    }
    log_f = open(job["log_file"], "w", encoding="utf-8")
    header = f"$ {job['cmd']}"
    log_f.write(header + "\n\n")
    log_f.flush()
    job["log_lines"] = [header]
    with JOBS_LOCK:
        JOBS[job_id] = job

    def worker():
        plan = {
            "repo": updater.UPDATE_REPO,
            "current": SERVER_VERSION,
            "latest": st.get("latest"),
            "asset": st.get("asset"),
            "bundle": _own_bundle(),
            "work": DATA_DIR / "update",
            "force": bool(force),
        }
        try:
            updater.run_job(job, plan, log_f)
        except Exception as e:
            import traceback
            job["log_lines"].append("    " + traceback.format_exc(limit=3).replace("\n", "\n    "))
            job["status"] = "fail"
            job["exit_code"] = 1
            job["ended"] = time.time()
            for q in list(job["subs"]):
                try:
                    q.put(("done", "fail", 1))
                except Exception:
                    pass
        finally:
            try:
                log_f.close()
            except Exception:
                pass
            _persist_jobs()

    threading.Thread(target=worker, daemon=True, name="update-install").start()
    return job, []



# ============================================================================
# Managed repo copies (see repo_sync.py)
# ============================================================================

_refs_cache = {}


def repos_view(settings: dict) -> list:
    """One display row per sister repo: where it lives, what it's at, what's asked."""
    st = repo_sync.load_state()
    prog = repo_sync.load_progress()
    # A row is only “live” while its writer is: accepted writes stamp ts (a
    # download tick every ~2% of transfer, a fingerprint heartbeat every ~2%
    # of files). A row older than 300 s is a leftover from a pass that never
    # reached its clear — a first boot that died mid-clone, for example — and
    # it must not masquerade as in-flight work on an already-deployed repo.
    # Rows without a stamp (written before stamps existed) are stale by the
    # same rule, which is what heals a first boot stuck on the old race.
    now = time.time()
    for k, v in list(prog.items()):
        if float(v.get("ts") or 0) <= 0 or now - float(v["ts"]) > 300:
            prog.pop(k, None)
    scm, extras = effective_dirs(settings)
    rows = []
    for key, meta in repo_sync.REPOS.items():
        r = st.get(key) or {}
        deployed = r.get("deployed")
        managed = bool(deployed) and r.get("mode") == "bundled"
        cfg = (settings.get("repos", {}) or {}).get(key) or {}
        source = r.get("source") or cfg.get("source") or meta.get("default_source", "main")
        pin = r.get("pin") or cfg.get("pin") or ""
        if source == "pinned" and pin:
            source = pin
        ext = scm if key == "scm" else extras
        if managed:
            path, mode = DATA_DIR / meta["rel"], "managed"
        elif ext:
            path, mode = ext, "external"
        else:
            path, mode = None, "missing"
        rows.append({
            "key": key, "name": meta["name"], "mode": mode, "path": str(path) if path else None,
            "source": source, "deployed": deployed, "last_check": r.get("last_check"),
            "progress": (prog.get(key) or None),
        })
    return rows


def run_repo_check(key: str, force: bool = False) -> dict:
    """In-process 'check for updates' (small API calls only). Results cache an hour."""
    try:
        return repo_sync.check_repo(key, force=force)
    except repo_sync.RepoError as e:
        return {"repo": key, "ok": False, "error": str(e)}


def effective_dirs(settings: dict) -> Tuple[Optional[Path], Optional[Path]]:
    def resolve(p: str) -> Optional[Path]:
        if not p:
            return None
        pp = Path(p)
        if not pp.is_absolute():
            pp = Path(__file__).resolve().parent / pp
        return pp if pp.is_dir() else None

    scm = resolve(settings.get("scm_dir") or "")
    extras = resolve(settings.get("extras_dir") or "")
    # A managed copy (downloaded by the Workbench itself) counts as the repo
    # when no explicit path is set — that's what makes a packaged app fully
    # self-contained. An explicit user path always wins; in a bare dev checkout
    # (no managed copies) the old sibling auto-detect applies.
    st = repo_sync.load_state()
    if not scm:
        r = st.get("scm") or {}
        if r.get("deployed"):
            p = repo_sync.repo_dir("scm")
            if p.is_dir():
                scm = p
    if not extras:
        r = st.get("extras") or {}
        if r.get("deployed"):
            p = repo_sync.repo_dir("extras")
            if p.is_dir():
                extras = p
    if not scm:
        scm = find_scm_repo()
    if not extras:
        extras = find_extras_repo()
    return scm, extras


# ============================================================================
# Offsets
#
# silhouette-card-maker keeps ONE global printer offset per repo
# (data/offset_data.json, read by create_pdf --load_offset and offset_pdf.py).
# The required correction, however, depends on the paper you feed — so the
# Workbench keeps a per-paper-size table of its own and *stages* the matching
# row into that shared file right before a run. SCM's code never changes;
# it just reads the one file it always knew about.
# ============================================================================

OFFSET_STAGE_LOCK = threading.Lock()


def load_per_size_offsets() -> dict:
    data = _try_read_json(PER_SIZE_OFFSETS_FILE)
    return data if isinstance(data, dict) else {}


def save_per_size_offsets(table: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PER_SIZE_OFFSETS_FILE, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=1)


def write_global_offset(scm: Optional[Path], x: int, y: int, angle: float) -> None:
    """Write SCM's shared data/offset_data.json (same shape SCM's own save_offset writes)."""
    if not scm:
        return
    d = scm / "data"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "offset_data.json", "w", encoding="utf-8") as f:
        json.dump({"x_offset": int(x), "y_offset": int(y), "angle_offset": float(angle)}, f, indent=4)


def read_global_offset(scm: Optional[Path]) -> Optional[dict]:
    if not scm:
        return None
    o = _try_read_json(scm / "data" / "offset_data.json")
    if not o:
        return None
    return {"x": o.get("x_offset", 0), "y": o.get("y_offset", 0), "angle": o.get("angle_offset", 0)}


def effective_paper(info: dict, kind: str, args: dict, settings: dict) -> Optional[str]:
    """The paper size a job would actually print on (a specialty layout wins over the pick)."""
    if kind == "create_pdf":
        d = settings.get("defaults", {})
        paper = str(args.get("paper_size") or d.get("paper_size") or "letter")
        sp = next((s for s in info.get("scm", {}).get("specialty", []) if s.get("name") == args.get("specialty")), None)
        if sp and sp.get("paper"):
            paper = sp["paper"]
        return paper or None
    if kind == "offset_pdf":
        return str(args.get("paper_size") or "") or None
    return None


def stage_per_size_offset(scm: Optional[Path], paper: Optional[str]) -> Optional[dict]:
    """Stage the per-size row for `paper` into SCM's shared offset file. Returns the row, or None."""
    if not paper or not scm:
        return None
    entry = load_per_size_offsets().get(paper)
    if not entry:
        return None
    with OFFSET_STAGE_LOCK:
        write_global_offset(scm, entry.get("x", 0), entry.get("y", 0), entry.get("angle", 0))
    return {"size": paper, "x": entry.get("x", 0), "y": entry.get("y", 0), "angle": entry.get("angle", 0)}


def _bootstrap_state() -> dict:
    """Live status of the launcher's first-launch preparation (flag file in
    the data area; absence = packaged-and-ready or dev checkout)."""
    try:
        f = DATA_DIR / "bootstrap.json"
        if f.is_file():
            d = json.loads(f.read_text(encoding="utf-8"))
            return {"active": bool(d.get("pending")), "phase": d.get("phase") or ""}
    except Exception:
        pass
    return {"active": False, "phase": ""}


def _bootstrapping() -> bool:
    return _bootstrap_state()["active"]


def release_notes_view() -> dict:
    """The newest release's notes, fetched fresh from GitHub for the
    in-app "What's new" view: the app itself renders them (release notes
    live on GitHub, not in the bundle, so the running app shows the
    release it was built for, even after the release page has moved on).
    Falls back to the stored update state when the network can't confirm."""

    import re as _re

    try:
        rel = updater.latest_release(timeout=15)
        tag, body, url = rel.get("tag") or "", rel.get("body") or "", rel.get("url") or ""
        published = rel.get("published") or ""
        name = rel.get("name") or tag
    except Exception:
        st = load_update_state()
        tag = st.get("latest") or ""
        body, url = "", st.get("release_url") or ""
        published = st.get("published") or ""
        name = tag

    if not tag:
        return {"ok": False, "error": "no release is known yet"}

    # Minimal, conservative markdown → html for release notes: headings,
    # paragraphs (with soft-wrap), bold, italic, code, links, lists, and
    # fenced code blocks. The notes are written by us, so this is
    # display-only, not a general renderer. Structure comes from blocks:
    # consecutive text lines merge into one <p> (markdown soft-wrap),
    # consecutive list lines into one <ul> — a <p> per raw line is what
    # made an earlier revision of this view read like broken prose.
    def inline(s: str) -> str:
        s = _re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                    lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', s)
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = _re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = _re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
        return s

    def md(src: str) -> str:
        if not src:
            return ""
        out = []
        para = []
        items = []
        fence = False
        code = []

        def flush_para():
            if para:
                out.append("<p>" + inline(" ".join(para)) + "</p>")
                para.clear()

        def flush_list():
            if items:
                out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
                items.clear()

        for raw in src.split("\n"):
            line = raw.rstrip()
            stripped = line.strip()
            if stripped.startswith("```"):
                if fence:
                    out.append("<pre><code>" + _re.sub(r"<[^>]+>", "", "\n".join(code)) + "</code></pre>")
                    code.clear()
                    fence = False
                else:
                    flush_para(); flush_list(); fence = True
                continue
            if fence:
                code.append(line)
                continue
            if not stripped:
                flush_para(); flush_list()
                continue
            if _re.match(r"^#{1,4}\s", stripped):
                flush_para(); flush_list()
                lvl = len(stripped) - len(stripped.lstrip("#"))
                out.append(f"<h{min(lvl + 1, 5)}>{inline(stripped.lstrip('#').strip())}</h{min(lvl + 1, 5)}>")
                continue
            m = _re.match(r"^[-*+]\s+(.*)$", stripped)
            if m:
                flush_para(); items.append(m.group(1)); continue
            flush_list(); para.append(stripped)
        if fence:  # an unclosed fence still shows its code
            out.append("<pre><code>" + _re.sub(r"<[^>]+>", "", "\n".join(code)) + "</code></pre>")
        flush_para(); flush_list()
        return "".join(out)

    when = ""
    if published:
        try:
            # "2026-09-03T21:03:21Z" -> (2026, 9, 3, 0, 0, 0, 0, 0, 0) for strftime
            p = published.split("T")
            d = p[0].split("-")
            t = p[1][:2].lstrip("0") or "0"
            when = time.strftime("%b %-d, %Y", (int(d[0]), int(d[1]), int(d[2]), int(t), 0, 0, 0, 0, 0))
        except Exception:
            when = published
    return {"ok": True, "tag": tag, "name": name, "published": when, "url": url, "body": md(body)}


def get_info() -> dict:

    settings = load_settings()
    scm, extras = effective_dirs(settings)
    return {
        "server": {
            "version": SERVER_VERSION,
            "python": sys.version.split()[0],
            "python_path": str(Path(sys.executable).resolve()),
            "platform": sys.platform,
            "is_windows": os.name == "nt",
            "is_packaged": os.environ.get("SCM_WORKBENCH_PACKAGED") == "1",
            **_bootstrap_state(),
            "runtime_ready": bool(os.environ.get("SCM_WORKBENCH_PYTHON")),
            "data_dir": str(DATA_DIR),
        },
        # the window host's state, when it is not the app's own window
        # (browser fallback): <data>/window.json, written by the launcher
        "window": _try_read_json(DATA_DIR / "window.json") or {},
        "scm": read_scm_info(scm, extras),
        "extras": read_extras_info(extras),
        "per_size_offsets": load_per_size_offsets(),
        "repos": repos_view(settings),
        "settings": settings,
    }


def _repos_signal_mtime() -> float:
    now = 0.0
    for p in (repo_sync.state_file(), DATA_DIR / "repos-manifest-scm.json"):
        try:
            now = max(now, p.stat().st_mtime)
        except OSError:
            pass
    try:
        scm, _ = effective_dirs(load_settings())
        if scm:
            dl = scm / "game" / "decklist"
            try:
                now = max(now, dl.stat().st_mtime)
            except OSError:
                pass
    except Exception:
        pass
    return now


def extras_card_names(info: dict) -> set:
    names = set()
    for c in info.get("extras", {}).get("card_sizes", []):
        names.add(c["name"].lower())
        for a in c.get("aliases", []):
            names.add(a.lower())
    return names


# ============================================================================
# Jobs
# ============================================================================

JOBS: Dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _line_wire(line: Any) -> Tuple[str, bool]:
    """Return a UTF-8 bounded line and whether it had to be clipped."""
    text = str(line)
    raw = text.encode("utf-8", "replace")
    if len(raw) <= JOB_LINE_MAX_BYTES:
        return text, False
    # Decode a byte prefix without splitting a code point and make the loss
    # visible to native callers rather than silently changing a transcript.
    marker = "… [line truncated]"
    room = max(1, JOB_LINE_MAX_BYTES - len(marker.encode("utf-8")))
    clipped = raw[:room].decode("utf-8", "ignore")
    return clipped + marker, True


def _job_lines_locked(job: dict) -> Tuple[int, List[str]]:
    """Copy transcript state while JOBS_LOCK is already held."""
    lines = list(job.get("log_lines") or [])
    return int(job.get("first_seq", 0) or 0), lines


def _job_lines_snapshot(job: dict) -> Tuple[int, List[str]]:
    """Copy transcript state without exposing a mutating list to readers."""
    with JOBS_LOCK:
        return _job_lines_locked(job)


def _append_job_line(job: dict, line: Any, *, log_f=None) -> int:
    """Append a complete line and wake subscribers without ever blocking."""
    text = str(line)
    if log_f is not None:
        log_f.write(text + "\n")
        log_f.flush()
    with JOBS_LOCK:
        lines = job.setdefault("log_lines", [])
        first = int(job.get("first_seq", 0) or 0)
        seq = first + len(lines)
        lines.append(text)
        subscribers = list(job.get("subs", []))
    _notify_subscribers(job, subscribers, ("line", seq, text))
    return seq


def _notify_subscribers(job: dict, subscribers: list, message: tuple, *, terminal: bool = False) -> None:
    """Non-blocking subscriber wakeups; a slow SSE client gets a replay gap."""
    for q in subscribers:
        try:
            q.put_nowait(message)
        except queue.Full:
            # Drop queued wakes, not the transcript.  The consumer will replay
            # from log_lines after the explicit gap marker.  A terminal marker
            # is forced in below so completion can never be lost.
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(("gap",))
            except queue.Full:
                pass
            if terminal:
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(message)
                except queue.Full:
                    pass


def _persist_jobs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        rows = sorted(JOBS.values(), key=lambda j: j["ts"], reverse=True)[:100]
    slim = [
        {k: j[k] for k in ("id", "ts", "kind", "title", "cmd", "args", "status", "exit_code", "log_file", "duration")}
        for j in rows if j["status"] != "running" and j.get("duration") is not None
    ]
    # Merge with rows persisted by earlier sessions: the in-memory map only
    # knows about *this* process's jobs, and rewriting the file from it alone
    # would silently erase the user's job history on every relaunch.
    try:
        old = _try_read_json(JOBS_FILE) or []
    except Exception:
        old = []
    ids = {s["id"] for s in slim}
    try:
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump((slim + [o for o in old if o.get("id") not in ids])[:100], f, indent=1)
    except Exception:
        pass


def read_persisted_jobs() -> list:
    data = _try_read_json(JOBS_FILE)
    return data or []


def list_jobs() -> dict:
    """The one authoritative shape used by HTTP and native callers."""
    with JOBS_LOCK:
        live = [dict(j) for j in sorted(JOBS.values(), key=lambda x: x["ts"], reverse=True)[:50]]
    running = []
    for j in live:
        row = {"id": j["id"], "ts": j["ts"], "kind": j["kind"], "title": j["title"],
               "status": j["status"], "exit_code": j.get("exit_code"), "cmd": j["cmd"]}
        if j.get("progress"):
            row["progress"] = j["progress"]
        row.update(warnings=j.get("warnings", []), outputs=job_outputs(j))
        running.append(row)
    ids = {r["id"] for r in running}
    history = []
    for old in read_persisted_jobs():
        if old.get("id") in ids:
            continue
        row = dict(old)
        row.setdefault("outputs", job_outputs(row))
        history.append(row)
    return {"jobs": running + history[:200]}


def _job_record(job_id: str) -> Optional[dict]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        snapshot = dict(job)
        snapshot["log_lines"] = list(job.get("log_lines") or [])
        return snapshot


def _persisted_record(job_id: str) -> Optional[dict]:
    return next((dict(j) for j in read_persisted_jobs() if j.get("id") == job_id), None)


def get_job_log(job_id: str, after: int = 0, max_lines: int = JOB_LOG_MAX_LINES,
                *, byte_limit: int = IPC_POLL_MAX_BYTES) -> dict:
    """Read a bounded cursor window shared by HTTP and native IPC."""
    job = _job_record(job_id) or _persisted_record(job_id)
    if not job:
        return {"lines": [], "status": "missing", "exit_code": None, "cmd": "",
                "first_seq": 0, "next_seq": 0, "truncated": False, "gap": False}
    first = int(job.get("first_seq", 0) or 0)
    lines = list(job.get("log_lines") or [])
    if not lines and job.get("log_file"):
        try:
            with open(job["log_file"], encoding="utf-8", errors="replace") as f:
                lines = [line.rstrip("\r\n") for line in f]
        except OSError:
            pass
    available_next = first + len(lines)
    requested = max(0, int(after))
    start = max(requested, first)
    truncated = requested < first
    selected = lines[start - first:]
    if len(selected) > max_lines:
        selected = selected[:max_lines]
        truncated = True
    wire, clipped = [], False
    # Add lines incrementally so a hostile transcript never causes an
    # quadratic serialize-and-pop loop. The one-byte list comma is included.
    base_size = len(json.dumps({"lines": [], "status": job.get("status", "missing"),
                                "exit_code": job.get("exit_code"), "cmd": job.get("cmd", ""),
                                "first_seq": first, "next_seq": start,
                                "truncated": False, "gap": bool(requested < first),
                                "line_truncated": False}, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8"))
    used = base_size
    for line in selected:
        bounded, was_clipped = _line_wire(line)
        item_size = len(json.dumps(bounded, ensure_ascii=False).encode("utf-8")) + (1 if wire else 0)
        if wire and used + item_size > byte_limit:
            truncated = True
            break
        wire.append(bounded)
        used += item_size
        clipped = clipped or was_clipped
    next_seq = max(start + len(wire), min(requested, available_next))
    return {"lines": wire, "status": job.get("status", "missing"),
            "exit_code": job.get("exit_code"), "cmd": job.get("cmd", ""),
            "first_seq": first, "next_seq": next_seq,
            "truncated": bool(truncated or clipped or len(wire) < len(selected)),
            "gap": bool(requested < first),
            "line_truncated": bool(clipped)}


def poll_jobs(cursors: list, max_events: int) -> dict:
    """Return independent bounded windows for native job polling."""
    result = []
    # Keep a little room for the IPC envelope. This is the complete frame
    # budget for the result, not a decrementing per-row budget.
    frame_budget = IPC_POLL_MAX_BYTES - 64 * 1024

    def encoded_size(rows: list) -> int:
        return len(json.dumps({"jobs": rows}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))

    for index, cursor in enumerate(cursors):
        jid, after = cursor["job_id"], cursor["after"]
        # Allocate the remaining frame budget fairly before reading any lines;
        # each helper also stops incrementally at this per-job byte budget.
        remaining = max(1, len(cursors) - index)
        used = encoded_size(result)
        remaining_budget = max(0, frame_budget - used)
        per_cursor = max(4096, remaining_budget // remaining)
        job = _job_record(jid) or _persisted_record(jid)
        if not job:
            row = {"job_id": jid, "lines": [], "next_seq": after,
                   "status": "missing", "exit_code": None, "cmd": "",
                   "complete": True, "truncated": False, "gap": False}
            result.append(row)
            continue
        log = get_job_log(jid, after, max_events, byte_limit=per_cursor)
        # get_job_log returns strings; native batches retain the established
        # SSE indexes and independently advance each cursor.
        first = log["first_seq"]
        indexed = [{"i": first + max(0, after - first) + i, "s": line}
                   for i, line in enumerate(log["lines"])]
        row = {"job_id": jid, "lines": indexed, "next_seq": log["next_seq"],
               "status": log["status"], "exit_code": log["exit_code"],
               "cmd": _line_wire(log["cmd"])[0], "complete": log["status"] != "running",
               "truncated": log["truncated"], "gap": log["gap"]}
        # A long line or many jobs can otherwise make max_events exceed the
        # frame budget. Remove tail events (never another job's events) and
        # report the resulting gap through truncated while preserving cursor.
        while indexed and encoded_size(result + [row]) > frame_budget:
            indexed.pop()
            row["lines"] = indexed
            row["truncated"] = True
            row["next_seq"] = max(first + len(indexed), min(after, log["next_seq"]))
        result.append(row)
    return {"jobs": result}


def job_outputs(job: dict) -> list:
    """Artifact file(s) of a job as absolute paths — what the console's
    “Move to my files…” button can carry out of the app's private working
    area: the create/offset PDFs and the calibration sheets. Older persisted
    jobs (no recorded form args) fall back to the default paths."""
    kind = job.get("kind")
    if kind not in ("create_pdf", "offset_pdf", "calibration"):
        return []
    args = job.get("args") or {}
    try:
        scm, _ = effective_dirs(load_settings())
    except Exception:
        return []
    if not scm:
        return []
    if kind == "create_pdf":
        if args.get("output_images"):
            return []
        p = Path(str(args.get("output_path") or "game/output/game.pdf"))
        return [str(p if p.is_absolute() else scm / p)]
    if kind == "offset_pdf":
        src = Path(str(args.get("pdf_path") or "game/output/game.pdf"))
        if not src.is_absolute():
            src = scm / src
        out = str(args.get("output_pdf_path") or "")
        if not out:
            out = str(src.with_name(src.stem + "_offset.pdf"))
        p = Path(out)
        return [str(p if p.is_absolute() else scm / p)]
    cdir = scm / "calibration"
    return [str(p) for p in sorted(cdir.glob("*.pdf"))] if cdir.is_dir() else []


def _utf8_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # scripts that print progress in a loop (the fetch plugins print a line
    # per batch of cards) otherwise sit in Python's 8 KB pipe buffer until the
    # process exits, so the UI sees the whole transcript as one chunk at the
    # end. Line-buffered stdout makes each line reach the console as it's made.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _proc_kwargs() -> dict:
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _external_proc_kwargs() -> dict:
    """Detach UI-launched helpers from the worker's protocol stdio."""
    return {**_proc_kwargs(), "shell": False,
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}


def _fmt_argv(argv: List[str]) -> str:
    return " ".join(shlex.quote(p) if " " in p else p for p in argv)


def bundled_python() -> Path:
    """The interpreter job scripts should run with.

    In a dev checkout that's simply sys.executable. Inside an app bundle,
    however, sys.executable is the launcher *stub* (on macOS a dylib that
    can't be exec'd; on Windows an executable that only re-launches the app,
    ignoring its arguments), so the launcher provisions a relocatable CPython
    in the data area and tells us where it is via SCM_WORKBENCH_PYTHON.
    """
    env_py = os.environ.get("SCM_WORKBENCH_PYTHON")
    if env_py:
        p = Path(env_py)
        if p.is_file():
            return p
    exe = Path(sys.executable)
    name = exe.name.lower()
    if "python" in name:
        return exe
    # Fallbacks: the in-bundle framework / a runtime beside the stub
    app = next((p for p in exe.parents if p.suffix == ".app" or p.name.endswith(".app")), None)
    if app is not None:
        cand = app / "Contents" / "Frameworks" / "Python.framework" / "Versions" / "Current" / "Python"
        if cand.is_file():
            return cand
    for cand in (exe.parent / "pythonw.exe", exe.parent / "python.exe"):
        if cand.is_file():
            return cand
    return exe


def app_packages_dir() -> Optional[Path]:
    """The bundle's support site-packages (Resources/app_packages), if any."""
    base = Path(__file__).resolve()
    for p in base.parents:
        if p.name.endswith(".app"):
            d = p / "Contents" / "Resources" / "app_packages"
            return d if d.is_dir() else None
    return None


def build_command(kind: str, args: dict, settings: dict, info: dict, write_deck: bool = True) -> Tuple[list, Optional[Path], dict, str, list, list]:
    """Assemble (argv, cwd, env, title, warnings, errors) for a job kind.

    The browser sends structured values only; argv is assembled here, in one
    place, which keeps command previews and real runs identical.
    """
    warnings: List[str] = []
    if os.environ.get("SCM_WORKBENCH_PACKAGED") and not os.environ.get("SCM_WORKBENCH_PYTHON"):
        warnings.append(
            "First launch: the app's private Python runtime is still being prepared in the "
            "background, so this job runs without the repo's packages and may fail on import. "
            "Give it a minute and try again — or watch the dashboard banner.")
    errors: List[str] = []
    scm, extras = effective_dirs(settings)
    python = bundled_python()
    if settings.get("python"):
        p = Path(settings["python"])
        p = p if p.is_absolute() else Path(__file__).resolve().parent / p
        if p.exists():
            python = p
        else:
            warnings.append(f"Configured python not found ({p}); using {python.name}.")
    # A job can never run on the app's own stub: on Windows the stub is a
    # fixed "run the app" binary, so Popen'ing it would launch another copy
    # of this app (which launches another, …). Until the private runtime is
    # provisioned, jobs decline to start and say why.
    if (os.environ.get("SCM_WORKBENCH_PACKAGED") and not os.environ.get("SCM_WORKBENCH_PYTHON")
            and os.name == "nt" and Path(str(python)).resolve() == Path(sys.executable).resolve()):
        errors.append(
            "the app's private Python runtime isn't ready yet (first launch) — and a job can't "
            "run on the app's own stub, because that would just launch another copy of the app. "
            "Try again in a minute; the dashboard banner tracks provisioning.")

    manifest = get_manifest()
    spec = manifest.get(kind) or {}
    title = spec.get("job_title") or spec.get("title") or kind
    env = _utf8_env()
    argv = [str(python)]

    def require_repo(name: str, path: Optional[Path], hint: str = "") -> bool:
        if path is None or not Path(path).is_dir():
            errors.append(f"{name} repo not found — set its path in Settings{'. ' + hint if hint else '.'}")
            return False
        return True

    d = settings.get("defaults", {})

    # Packaged apps: the job's (provisioned) interpreter isn't the one that
    # installed the support packages, so point it at them explicitly.
    if os.environ.get("SCM_WORKBENCH_PACKAGED"):
        pkgs = app_packages_dir()
        if pkgs:
            env["PYTHONPATH"] = str(pkgs)

    if kind in ("repo_update", "repo_init"):
        cwd = WB_ROOT
        a = args
        argv += ["-m", "scm_workbench.repo_sync", kind.split("_")[-1], "--repo", str(a.get("repo") or "scm")]
        if kind == "repo_update" and a.get("force_full"):
            argv += ["--force-full"]
        env["SCM_WORKBENCH_DATA"] = str(DATA_DIR)

    elif kind == "create_pdf":
        if not require_repo("SCM", scm, "e.g. the silhouette-card-maker folder."):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        # In simple mode the command shows only what deviates from the
        # defaults: SCM's own defaults (the folder paths, 3-mark
        # registration, stretch fit) never need to appear in the command a
        # user reads — unless the value actually deviates (edited in
        # advanced mode, say) or, for quality, the global quality setting
        # is other than 100.
        simple = str(settings.get("ui_mode", "advanced")) == "simple"

        def emit(key, *flag, default=None):
            nonlocal argv
            v = a.get(key)
            if v in (None, ""):
                return
            if simple and default is not None and str(v) == default:
                return
            argv += list(flag) + [str(v)]

        argv += ["create_pdf.py"]
        emit("front_dir", "--front_dir_path", default="game/front")
        emit("back_dir", "--back_dir_path", default="game/back")
        emit("double_sided_dir", "--double_sided_dir_path", default="game/double_sided")
        argv += ["--output_path", str(a.get("output_path") or "game/output/game.pdf")]
        if a.get("output_images"): argv += ["--output_images"]
        card = str(a.get("card_size") or d.get("card_size") or "standard")
        paper = str(a.get("paper_size") or d.get("paper_size") or "letter")
        argv += ["--card_size", card, "--paper_size", paper]
        emit("registration", "--registration", default="3")
        if a.get("registration_orientation"): argv += ["--registration_orientation", str(a["registration_orientation"])]
        if a.get("specialty"): argv += ["--specialty", str(a["specialty"])]
        if a.get("only_fronts"):
            argv += ["--only_fronts"]
            ds = str(a.get("double_sided_dir") or "")
            if ds:
                ds_dir = (cwd / ds) if not Path(ds).is_absolute() else Path(ds)
                if ds_dir.is_dir():
                    n = sum(1 for c in ds_dir.iterdir() if c.is_file() and is_image_file(c))
                    if n:
                        warnings.append(
                            f"Double-sided folder “{ds}” still has {n} image{'s' if n == 1 else 's'} — "
                            "create_pdf.py refuses --only_fronts while those exist; remove them first or uncheck the option.")
        emit("fit", "--fit", default="stretch")
        if a.get("fit_backs"): argv += ["--fit_backs", str(a["fit_backs"])]
        for key in ("crop", "crop_backs", "extend_edges", "extend_edges_backs",
                    "extend_corners", "extend_corners_backs", "extend_bleed", "extend_bleed_backs"):
            v = a.get(key)
            if key == "crop" and not v and a.get("mpcfill_crop") and simple:
                # the simple-mode “MPCFill Crop” toggle is shorthand for a 3mm
                # crop: MPCFill's fetched art carries its own print-bleed
                # padding. In advanced mode the toggle is not offered at all -
                # the Crop boxes are the direct control - so a leftover value
                # can't silently crop a PDF, and a typed value always wins.
                v = "3mm"
            if v: argv += ["--" + key, str(v)]
        ppi = a.get("ppi")
        ppi = int(ppi) if ppi not in (None, "") else int(d.get("ppi", 1200))
        quality = a.get("quality")
        quality = int(quality) if quality not in (None, "") else int(d.get("quality", 100))
        if simple and quality == 100:
            # the form sat at the manifest default — the global quality
            # setting is the preference that governs
            quality = int(d.get("quality", 100))
        argv += ["--ppi", str(ppi)]
        if not (simple and quality == 100):
            argv += ["--quality", str(quality)]
        for idx in a.get("skip") or []:
            argv += ["--skip", str(idx)]
        if a.get("label"): argv += ["--label", str(a["label"])]
        if a.get("show_outline"): argv += ["--show_outline"]
        if a.get("borderless"): argv += ["--borderless"]
        if a.get("load_offset"):
            argv += ["--load_offset"]
            paper_eff = effective_paper(info, "create_pdf", a, settings)
            entry = load_per_size_offsets().get(paper_eff or "")
            g = info["scm"].get("saved_offset")
            if entry:
                warnings.append(
                    f"Per-size offset for “{paper_eff}” (x {entry['x']}, y {entry['y']}, {entry['angle']}°) "
                    "will be staged into data/offset_data.json before the run — SCM keeps one shared offset file, "
                    "so this job prints with that row.")
            elif g:
                warnings.append(f"No per-size offset saved for “{paper_eff}” — the global saved offset (x {g['x']}, y {g['y']}, {g['angle']}°) applies.")
            else:
                warnings.append("No offset saved (global or per-size) — “--load_offset” has nothing to apply.")
        known_extra = extras_card_names(info)
        for v in (card, paper):
            if v and v.lower() in known_extra:
                extra_file = (extras / "assets" / "layouts_extra.json") if extras else None
                if extra_file and extra_file.is_file():
                    env["SCM_EXTRA_LAYOUTS"] = str(extra_file)
                    warnings.append(f"Extras size “{v}” detected — SCM_EXTRA_LAYOUTS is set automatically.")
                else:
                    errors.append(f"“{v}” comes from scm-extras, but its layouts file can’t be found.")
                break

    elif kind == "offset_pdf":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        src = a.get("pdf_path") or "game/output/game.pdf"
        argv += ["offset_pdf.py", "--pdf_path", str(src)]
        if a.get("output_pdf_path"):
            argv += ["--output_pdf_path", str(a["output_pdf_path"])]
        gave_any = False
        for k, f in (("x_offset", "-x"), ("y_offset", "-y"), ("angle", "-a")):
            if a.get(k) not in (None, ""):
                argv += [f, str(a[k])]
                gave_any = True
        if not gave_any:
            errors.append("Provide at least one of X, Y, or angle.")
        argv += ["--ppi", str(int(a.get("ppi") or 1200))]
        if a.get("save"):
            argv += ["-s"]
        if a.get("paper_size"):
            entry = load_per_size_offsets().get(str(a["paper_size"]))
            if entry:
                tail = " “Save” (−s) records the used values back into that row." if a.get("save") else ""
                warnings.append(
                    f"Per-size offset for “{a['paper_size']}” (x {entry['x']}, y {entry['y']}, {entry['angle']}°) is staged in "
                    f"before the run, so any field left blank falls back to that row instead of the global value.{tail}")

    elif kind == "calibration":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        argv += ["generate_calibration.py"]

    elif kind == "dxf_single":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        argv += ["generate_dxf.py", "single"]
        if (a.get("card_mode") or "named") == "named":
            card = str(a.get("card_size") or "standard")
            argv += ["--card_size", card]
            if a.get("card_name"):
                argv += ["--card_name", str(a["card_name"])]
        else:
            if not a.get("card_width") or not a.get("card_height"):
                errors.append("Custom card size: both width and height are required (e.g. 63mm and 88mm).")
                card = None
            else:
                card = f"{a['card_width']}x{a['card_height']}"
                argv += ["--card_width", str(a["card_width"]), "--card_height", str(a["card_height"])]
                if a.get("card_radius"):
                    argv += ["--card_radius", str(a["card_radius"])]
        if (a.get("paper_mode") or "named") == "named":
            paper = str(a.get("paper_size") or "letter")
            argv += ["--paper_size", paper]
            if a.get("paper_name"):
                argv += ["--paper_name", str(a["paper_name"])]
        else:
            if not a.get("paper_width") or not a.get("paper_height"):
                errors.append("Custom paper size: both width and height are required (e.g. 8.5in and 11in).")
                paper = None
            else:
                paper = f"{a['paper_width']}x{a['paper_height']}"
                argv += ["--paper_width", str(a["paper_width"]), "--paper_height", str(a["paper_height"])]
        variant = str(a.get("variant") or "default")
        argv += ["--variant", variant]
        argv += ["--orientation", str(a.get("orientation") or "optimize")]
        out = a.get("output_path")
        if not out:
            sub = "borderless/dxf" if variant == "borderless" else "dxf"
            card_lbl = card or "?"
            paper_lbl = paper or "?"
            vtag = "" if variant == "default" else f"-{variant}"
            out = f"cutting_templates/{sub}/{paper_lbl}-{card_lbl}{vtag}-v1.dxf"
        argv += [str(out)]
        if a.get("save"):
            argv += ["--save"]
        known_extra = extras_card_names(info)
        for v in (card, paper):
            if v and str(v).lower() in known_extra:
                extra_file = (extras / "assets" / "layouts_extra.json") if extras else None
                if extra_file and extra_file.is_file():
                    env["SCM_EXTRA_LAYOUTS"] = str(extra_file)
                    warnings.append(f"Extras size “{v}” detected — SCM_EXTRA_LAYOUTS is set automatically.")
                else:
                    errors.append(f"“{v}” comes from scm-extras, but its layouts file can’t be found.")
                break

    elif kind == "dxf_batch":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        argv += ["generate_dxf.py", "batch"]
        mode = str(args.get("mode") or "missing")
        if mode == "all":
            argv += ["--all"]
        elif mode == "optimize":
            argv += ["--optimize"]

    elif kind == "dxf_list":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        argv += ["generate_dxf.py", "list"]

    elif kind == "clean_up":
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        # the Workbench's own variant of SCM's clean_up.py (which would also
        # delete the README.md placeholders the current repo versions ship):
        # same clears, placeholders survive
        argv += [str(Path(__file__).resolve().parent / "clear_images.py")]

    elif kind == "extras_generate":
        if not require_repo("scm-extras", extras):
            return argv, None, env, title, warnings, errors
        cwd = extras
        argv += ["generate.py"]
        if str(args.get("mode") or "missing") == "all":
            argv += ["--all"]

    elif kind == "extras_tables":
        if not require_repo("scm-extras", extras):
            return argv, None, env, title, warnings, errors
        cwd = extras
        argv += ["generate_readme_tables.py"]

    elif kind.startswith("fetch:"):
        if not require_repo("SCM", scm):
            return argv, None, env, title, warnings, errors
        cwd = scm
        slug = kind.split(":", 1)[1]
        a = args
        fmt = str(a.get("format") or "")
        if not fmt:
            errors.append("Pick a decklist format.")
        deck_source = str(a.get("deck_source") or "file")
        deck = ""
        is_url_format = fmt == "url" or fmt.endswith("_url")
        if deck_source == "paste":
            content = str(a.get("deck_text") or "").strip()
            if not content:
                errors.append("Paste the decklist text.")
            name = str(a.get("deck_name") or "").strip() or f"{slug}_deck.txt"
            if not re.fullmatch(r"[\w .\-(\)]+", name):
                errors.append("Decklist file name may only contain letters, numbers, spaces and . - ( )")
            else:
                deckdir = cwd / "game" / "decklist"
                if write_deck:
                    deckdir.mkdir(parents=True, exist_ok=True)
                    (deckdir / name).write_text(content + "\n", encoding="utf-8")
                deck = f"game/decklist/{name}"
        elif deck_source == "url":
            deck = str(a.get("deck_url") or "").strip()
            if not re.fullmatch(r"https?://\S+", deck):
                errors.append("Enter a URL starting with http:// or https://.")
            elif not is_url_format:
                warnings.append(f"“{fmt}” reads a decklist file — a URL only works with URL-based formats (e.g. “url”, “*_url”).")
        else:
            name = str(a.get("deck_file") or "").strip()
            if not name:
                errors.append("Pick an existing decklist file (or use paste / URL).")
            elif "/" in name or "\\" in name:
                errors.append("Decklist names come from the game/decklist/ list — they can't contain path separators.")
            elif not (cwd / "game" / "decklist" / name).is_file():
                errors.append(f"Decklist file “{name}” is not in game/decklist/ — reopen the form to refresh the list.")
            else:
                # the plugin opens its argument relative to the repo root, so the
                # file source gets the same prefixed path as the paste source
                deck = f"game/decklist/{name}"
            if deck and is_url_format:
                warnings.append("URL-based formats use the URL itself, not a file — switch the source to “URL”.")
        fetch_script = cwd / "plugins" / slug / "fetch.py"
        if not fetch_script.is_file():
            errors.append(f"plugins/{slug}/fetch.py is not present in your SCM checkout — the job cannot run.")
        argv += [f"plugins/{slug}/fetch.py", deck, fmt]
        if slug == "mtg":
            if a.get("ignore_set_and_collector_number"): argv += ["-i"]
            if a.get("prefer_older_sets"): argv += ["--prefer_older_sets"]
            for s in a.get("prefer_set") or []: argv += ["--prefer_set", str(s)]
            for s in a.get("ignore_set") or []: argv += ["--ignore_set", str(s)]
            if a.get("prefer_showcase"): argv += ["--prefer_showcase"]
            if a.get("prefer_extra_art"): argv += ["--prefer_extra_art"]
            for l in a.get("prefer_lang") or []: argv += ["--prefer_lang", str(l)]
            if a.get("prefer_ub"): argv += ["--prefer_ub"]
            if a.get("ignore_ub"): argv += ["--ignore_ub"]
            if a.get("tokens"): argv += ["--tokens"]

        # The plugins never delete what they find in game/front/: re-fetching a
        # *different* deck silently leaves the old images behind, and the next
        # Create PDF would mix them into the layout. Say so before the run.
        front = cwd / "game" / "front"
        if front.is_dir():
            n = sum(1 for c in front.iterdir() if c.is_file() and is_image_file(c))
            if n:
                warnings.append(
                    f"The front folder already holds {n} image{'s' if n != 1 else ''} from a previous fetch. "
                    "Fetching overwrites matching files but never deletes anything — if this is a different deck, "
                    "clear the folder first (the “Clear card images” button on this page) or the old art ends up "
                    "in your next PDF."
                )

    else:
        errors.append(f"Unknown job kind: {kind}")
        return argv, None, env, title, warnings, errors

    return argv, cwd, env, title, warnings, errors


MANIFEST_CACHE: Dict[str, dict] = {}
MANIFEST_LOCK = threading.Lock()
_REPOS_MTIME: Dict[str, float] = {}


def _repos_changed() -> bool:
    """True when a repo update touched files the manifest reads (layouts.json etc.)."""
    now = None
    for p in (repo_sync.state_file(), DATA_DIR / "repos-manifest-scm.json"):
        try:
            now = max(now or 0, p.stat().st_mtime)
        except OSError:
            pass
    # the decklist folder feeds the deck_file choices — a file added or removed
    # there (Finder, paste-save, import) must invalidate the cached manifest
    try:
        scm, _ = effective_dirs(load_settings())
        if scm:
            dl = scm / "game" / "decklist"
            try:
                now = max(now or 0, dl.stat().st_mtime)
            except OSError:
                pass
    except Exception:
        pass
    if now is None:
        return False
    return now > _REPOS_MTIME.get("t", 0)


# The manifest and the preview share ONE repo snapshot: boot pays for the one
# full get_info() scan, and every keystroke-driven preview after that reads the
# same cached dict (30 s TTL, invalidating early on the same repo-change
# signals as the manifest) instead of re-walking both repos. The lock makes a
# burst of concurrent previews wait for one build rather than each scanning.
_INFO_SNAP: Dict[str, Any] = {}


def _repos_changed_since(t: float) -> bool:
    return _repos_signal_mtime() > t


def _get_info_locked() -> dict:
    """Build or return the shared repo snapshot (caller holds MANIFEST_LOCK)."""
    now = time.time()
    c = _INFO_SNAP
    if c.get("v") and now - c.get("t", 0) < 30 and not _repos_changed_since(c.get("t", 0)):
        return c["v"]
    v = get_info()
    c.clear()
    c.update(t=now, v=v)
    return v


def get_info_cached() -> dict:
    """The snapshot the preview endpoint runs against (see _INFO_SNAP)."""
    with MANIFEST_LOCK:
        return _get_info_locked()


def get_manifest() -> dict:
    with MANIFEST_LOCK:
        # The mtime signal alone can never fire again once the first build
        # happens after the last state write (exactly what a first boot looks
        # like: the cache is built empty at startup, the bootstrap then writes
        # state, and no file ever changes again) — so the cache also carries
        # the same 30 s TTL as the shared repo snapshot. Worst case a stale
        # manifest is visible for half a minute; rebuilding is cheap because it
        # rides on the snapshot.
        now = time.time()
        if (not MANIFEST_CACHE or now - _REPOS_MTIME.get("t", 0) > 30 or _repos_changed()):
            MANIFEST_CACHE.clear()
            MANIFEST_CACHE.update(build_manifest(_get_info_locked()))
            _REPOS_MTIME["t"] = now
    return MANIFEST_CACHE


def invalidate_manifest_cache() -> None:
    with MANIFEST_LOCK:
        MANIFEST_CACHE.clear()


class PreviewError(Exception):
    """A user-facing preview request error shared by HTTP and native IPC."""

    def __init__(self, *, status: int, http_body: dict, ipc_code: str, message: str):
        super().__init__(message)
        self.status = status
        self.http_body = http_body
        self.ipc_code = ipc_code
        self.message = message


def normalize_args(spec: dict, raw: dict) -> Tuple[dict, List[str], List[str]]:
    """Coerce/validate raw client values against the manifest. Returns (args, errors, warnings)."""
    errors: List[str] = []
    warns: List[str] = []
    args: Dict[str, Any] = {}
    for g in spec.get("groups", []):
        for o in g["options"]:
            key, t = o["key"], o["type"]
            v = raw.get(key)
            if t == "chips":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                v = v if isinstance(v, list) else []
                if o.get("int"):
                    clean = []
                    for x in v:
                        if re.fullmatch(r"\d+", str(x)):
                            clean.append(int(x))
                        else:
                            errors.append(f"{o['label']}: “{x}” is not a valid index.")
                    v = clean
                args[key] = v
            elif t == "choice_chips":
                if isinstance(v, str):
                    v = [x.strip() for x in v.split(",") if x.strip()]
                v = v if isinstance(v, list) else []
                valid = [c[0] for c in o.get("choices", [])]
                if valid:
                    v = [x for x in v if x in valid]
                args[key] = v
            elif t == "toggle":
                args[key] = bool(v)
            elif t == "textarea":
                args[key] = "" if v is None else str(v)
            elif t in ("number",):
                if v in (None, ""):
                    args[key] = o.get("default")
                else:
                    try:
                        f = float(v)
                        args[key] = int(f) if (o.get("step") in (None, 1) or f == int(f)) else round(f, 2)
                    except (TypeError, ValueError):
                        errors.append(f"{o['label']}: “{v}” is not a number.")
                        args[key] = o.get("default")
            elif t == "range":
                try:
                    f = float(v)
                    lo, hi = o.get("min", -1e9), o.get("max", 1e9)
                    if not (lo <= f <= hi):
                        warns.append(f"{o['label']} {int(round(f)) if (o.get('step') or 1) >= 1 else f} is outside the slider range ({lo:g}–{hi:g}) — the slider shows the nearest value, but the number you typed is used.")
                    args[key] = int(round(f)) if (o.get("step") or 1) >= 1 else round(f, 2)
                except (TypeError, ValueError):
                    args[key] = o.get("default")
            elif t in ("select", "segment"):
                sv = "" if v is None else str(v).strip()
                if not sv:
                    args[key] = o.get("default")
                else:
                    valid = [c[0] for c in o.get("choices", [])]
                    if valid and sv not in valid:
                        errors.append(f"{o['label']}: unknown value “{sv}”.")
                        args[key] = o.get("default")
                    else:
                        args[key] = sv
            else:  # text / path
                args[key] = "" if v is None else str(v).strip()
    return args, errors, warns


def build_preview(kind: str, raw_args: dict) -> dict:
    """Build the command preview used by both HTTP and native IPC.

    This deliberately shares the manifest, argument normalization, command
    builder, and cached repo snapshot used by jobs.  It only assembles a
    command: ``write_deck=False`` keeps preview requests side-effect free.
    """
    manifest = get_manifest()
    if kind not in manifest:
        raise PreviewError(
            status=404, http_body={"error": "unknown kind"},
            ipc_code="bad_request", message="unknown kind",
        )
    if not isinstance(raw_args, dict):
        raise PreviewError(
            status=400, http_body={"error": "bad args"},
            ipc_code="bad_request", message="preview args must be an object",
        )

    normalized, errors, norm_warns = normalize_args(manifest[kind], raw_args)
    settings = load_settings()
    argv, cwd, env, title, warnings, errs = build_command(
        kind, normalized, settings, get_info_cached(), write_deck=False,
    )
    # Create PDF needs card images to work with — the front directory
    # (SCM's own default when the form leaves it empty) empty means the
    # job would produce nothing, so the client keeps the run button
    # disabled until it has images.
    no_front = False
    if kind == "create_pdf" and not errs and cwd:
        front = normalized.get("front_dir") or "game/front"
        fd = Path(front) if os.path.isabs(front) else (Path(cwd) / front)
        n = sum(1 for c in fd.iterdir() if c.is_file() and is_image_file(c)) if fd.is_dir() else 0
        if n == 0:
            no_front = True
            # the alternate tip only exists in advanced mode — simple mode
            # can't change the front directory, so fetching is the only way
            # to run
            tip = "" if str(settings.get("ui_mode", "advanced")) == "simple" else " or point the form at a folder that has images."
            warnings.append(f"No images in the front directory ({front}). Use the fetch card art workflow first{tip or '.'}")
    return {
        # Always show the command that was built: validation problems are
        # already visible in the notes below, and a (partial or
        # default-substituted) command is the most useful thing on screen.
        "cmd": _fmt_argv(argv),
        "cwd": str(cwd) if cwd else None,
        "env": {k: v for k, v in env.items()
                if k.startswith("SCM_") or k in ("PYTHONIOENCODING", "PYTHONUTF8")},
        "warnings": warnings + norm_warns + errors,
        "errors": errs,
        "no_front_images": no_front,
    }


def start_job(kind: str, raw_args: dict) -> Tuple[Optional[dict], List[str]]:
    spec = get_manifest().get(kind)
    if not spec:
        return None, [f"Unknown job kind “{kind}”."]

    # client may send hidden selections outside the manifest (e.g. dxf_single's
    # card_mode-paired select) — keep them if present
    args, errors, norm_warns = normalize_args(spec, raw_args)

    info = get_info()
    argv, cwd, env, title, warnings, errs = build_command(kind, args, load_settings(), info)
    warnings += norm_warns
    errors += errs
    if errors:
        return None, errors

    # Stage the per-paper-size offset into SCM's shared file before the process
    # starts: SCM reads data/offset_data.json mid-run (its only supported shape),
    # so “per size” is realized by the Workbench picking which value goes in it.
    staged = None
    if kind == "create_pdf" and args.get("load_offset"):
        staged = stage_per_size_offset(cwd, effective_paper(info, kind, args, load_settings()))
    elif kind == "offset_pdf" and args.get("paper_size"):
        staged = stage_per_size_offset(cwd, str(args["paper_size"]))

    job_id = uuid.uuid4().hex[:10]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    job: dict = {
        "id": job_id,
        "ts": time.time(),
        "kind": kind,
        "title": title,
        "cmd": _fmt_argv(argv),
        "args": args,
        "status": "running",
        "exit_code": None,
        "log_file": str(LOGS_DIR / f"{job_id}.log"),
        "log_lines": [],
        "first_seq": 0,
        "subs": [],
        "warnings": warnings,
        "started": time.time(),
        "ended": None,
        "duration": None,
        "proc": None,
        "pump_thread": None,
    }
    if kind == "offset_pdf" and args.get("save") and args.get("paper_size"):
        # SCM's own -s writes the shared file with the values just used;
        # _pump mirrors them back into this row once the job has finished.
        job["offset_sync"] = str(args["paper_size"])
    log_f = open(job["log_file"], "w", encoding="utf-8")
    header = [f"$ {job['cmd']}", f"(cwd: {cwd})",
              f"(started {time.strftime('%Y-%m-%d %H:%M:%S')})"]
    if staged:
        header.append(f"(offset: staged “{staged['size']}” — x {staged['x']}, y {staged['y']}, {staged['angle']}° → data/offset_data.json)")
    log_f.write("\n".join(header) + "\n\n")
    log_f.flush()
    job["log_lines"] = header
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_proc_kwargs())
        job["proc"] = proc
        pump_thread = threading.Thread(
            target=_pump, args=(job, proc, log_f), daemon=True,
            name=f"job-pump-{job_id}",
        )
        with JOBS_LOCK:
            job["pump_thread"] = pump_thread
            JOBS[job_id] = job
        pump_thread.start()
    except Exception as e:
        log_f.write(f"failed to start: {e}\n")
        log_f.close()
        job["status"] = "fail"
        job["log_lines"].append(f"failed to start: {e}")
        with JOBS_LOCK:
            JOBS[job_id] = job
    return job, []


def _pump(job: dict, proc: subprocess.Popen, log_f) -> None:
    for line in iter(proc.stdout.readline, b""):
        s = line.decode("utf-8", "replace").rstrip("\r\n")
        _append_job_line(job, s, log_f=log_f)
    rc = proc.wait()
    with JOBS_LOCK:
        kill_requested = bool(job.get("kill_requested"))
        pump_lines = list(job.get("log_lines") or [])
    if kill_requested:
        status = "killed"
    elif rc == 0 and any(re.search(r"is not a valid file", l, re.IGNORECASE) for l in pump_lines):
        # most fetch plugins report a missing decklist this way and still exit
        # cleanly — a clean exit containing that line is a failed run
        status = "fail"
    elif rc == 0:
        status = "ok"
    else:
        status = "fail"
    with JOBS_LOCK:
        job["status"] = status
        job["exit_code"] = rc
        job["ended"] = time.time()
        job["duration"] = round(job["ended"] - job["started"], 2)
    if job.get("offset_sync"):
        # The run saved via SCM's own -s: mirror the shared file's new values
        # into this paper size's row in the Workbench's table.
        g = read_global_offset(effective_dirs(load_settings())[0])
        if g:
            table = load_per_size_offsets()
            table[job["offset_sync"]] = {"x": g["x"], "y": g["y"], "angle": g["angle"]}
            save_per_size_offsets(table)
            s = f"(offset: recorded the saved values in the “{job['offset_sync']}” row — x {g['x']}, y {g['y']}, {g['angle']}°)"
            _append_job_line(job, s, log_f=log_f)
    log_f.close()
    with JOBS_LOCK:
        subscribers = list(job.get("subs", []))
    _notify_subscribers(job, subscribers, ("done", status, rc), terminal=True)
    if job.get("kind", "").startswith("fetch:"):
        invalidate_manifest_cache()
    _persist_jobs()


def kill_job(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "running" or job.get("proc") is None:
            return False
        job["kill_requested"] = True
        proc = job["proc"]
    try:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
        else:
            # CREATE_NEW_PROCESS_GROUP is retained by _proc_kwargs; terminate
            # is the portable Windows fallback for controlled child fixtures.
            proc.terminate()
    except Exception:
        pass
    return True


def stop_all_jobs(timeout: float = 2.0) -> None:
    """Terminate/reap children and join pumps before a transport exits.

    The lock is used only to take the work list. Waiting while holding it would
    deadlock a pump trying to publish its terminal status.
    """
    with JOBS_LOCK:
        active = []
        for job in JOBS.values():
            pump = job.get("pump_thread")
            if job.get("status") == "running" or (pump is not None and getattr(pump, "is_alive", lambda: False)()):
                active.append(job)
    for job in active:
        if job.get("status") == "running":
            kill_job(job.get("id", ""))

    deadline = time.monotonic() + max(0.0, timeout)
    for job in active:
        proc = job.get("proc")
        if proc is None or not hasattr(proc, "wait"):
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
        except Exception:
            pass

    # Reaping closes the child's stdout, allowing _pump to finish its final
    # log write, subscriber wake, and persistence update. Join outside the
    # global lock so those operations can acquire it freely.
    pump_deadline = time.monotonic() + max(0.0, timeout)
    for job in active:
        pump = job.get("pump_thread")
        if pump is None or pump is threading.current_thread() or not hasattr(pump, "join"):
            continue
        try:
            pump.join(timeout=max(0.0, pump_deadline - time.monotonic()))
        except Exception:
            pass


def sse_stream(job_id: str, after: int):
    """Yield (event, data) tuples for one SSE subscriber of a job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        yield "done", json.dumps({"status": "missing"})
        return
    q = _WakeQueue(maxsize=SSE_QUEUE_SIZE)
    with JOBS_LOCK:
        job.setdefault("subs", []).append(q)
        first, initial = _job_lines_locked(job)
        status = job.get("status", "missing")
        exit_code = job.get("exit_code")
    sent = after - 1
    try:
        for offset, line in enumerate(initial):
            seq = first + offset
            if seq >= after:
                bounded, _ = _line_wire(line)
                yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                sent = seq
        if status != "running":
            yield "done", json.dumps({"status": status, "exit_code": exit_code})
            return
        while True:
            try:
                msg = q.get(timeout=15)
            except queue.Empty:
                with JOBS_LOCK:
                    status = job.get("status", "missing")
                    exit_code = job.get("exit_code")
                yield "ping", "{}"
                if status != "running":
                    # The terminal notification may have raced this timeout;
                    # replay from the authoritative transcript before done.
                    first, lines = _job_lines_snapshot(job)
                    for n, line in enumerate(lines):
                        seq = first + n
                        if seq > sent:
                            bounded, _ = _line_wire(line)
                            yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                            sent = seq
                    yield "done", json.dumps({"status": status, "exit_code": exit_code})
                    return
                continue
            if msg[0] == "gap":
                first, lines = _job_lines_snapshot(job)
                for n, line in enumerate(lines):
                    seq = first + n
                    if seq > sent:
                        bounded, _ = _line_wire(line)
                        yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                        sent = seq
            elif msg[0] == "line":
                # Legacy in-process update jobs still emit (line, text),
                # while subprocess jobs carry an authoritative sequence.
                if len(msg) >= 3:
                    seq, line = msg[1], msg[2]
                else:
                    seq, line = sent + 1, msg[1]
                if seq > sent:
                    bounded, _ = _line_wire(line)
                    yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                    sent = seq
            elif msg[0] == "done":
                first, lines = _job_lines_snapshot(job)
                for n, line in enumerate(lines):
                    seq = first + n
                    if seq > sent:
                        bounded, _ = _line_wire(line)
                        yield "line", json.dumps({"i": seq, "s": bounded}, ensure_ascii=False, separators=(",", ":"))
                        sent = seq
                with JOBS_LOCK:
                    final_status = job.get("status")
                    final_exit_code = job.get("exit_code")
                yield "done", json.dumps({"status": final_status, "exit_code": final_exit_code})
                return
    finally:
        with JOBS_LOCK:
            try:
                job["subs"].remove(q)
            except (ValueError, KeyError):
                pass


# ============================================================================
# File sandbox
# ============================================================================

# Native OS actions accept only bounded, printable values. Keep these limits
# independent of the JSON-lines frame limit and the HTTP request parser.
ACTION_PATH_MAX_BYTES = 4096
ACTION_URL_MAX_BYTES = 8192
ACTION_ERROR_MAX_BYTES = 4096


def has_forbidden_action_controls(value: str) -> bool:
    """Whether *value* contains a C0 (or DEL) control character."""
    return any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)


def _bounded_action_error(error: Any) -> str:
    """Make launcher errors safe to put in either action response surface."""
    text = str(error).replace("\r", " ").replace("\n", " ")
    # HTTP action responses use the default JSON ASCII escaping. Restricting
    # diagnostics to printable ASCII makes the byte bound hold on both HTTP
    # and native serialization paths, including Unicode OSError messages.
    text = "".join(char if 0x20 <= ord(char) < 0x7f else " " for char in text)
    if not text:
        text = "action failed"
    encoded = text.encode("ascii")
    if len(encoded) <= ACTION_ERROR_MAX_BYTES:
        return text
    return encoded[:ACTION_ERROR_MAX_BYTES].decode("ascii") or "action failed"


def _action_response(error: Optional[Any] = None) -> dict:
    if error is None:
        return {"ok": True, "errors": []}
    return {"ok": False, "errors": [_bounded_action_error(error)]}


class _ActionFailure(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message = message
        self.status = status


def _validate_action_value(value: Any, name: str, max_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise _ActionFailure(f"{name} must be a non-empty string", 400)
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _ActionFailure(f"{name} must be valid UTF-8", 400) from error
    if encoded_size > max_bytes:
        raise _ActionFailure(f"{name} exceeds {max_bytes} UTF-8 bytes", 400)
    if has_forbidden_action_controls(value):
        raise _ActionFailure(f"{name} contains control characters", 400)
    return value


def _action_path(raw: Any, operation: str, settings: Optional[dict] = None) -> Path:
    value = _validate_action_value(raw, "path", ACTION_PATH_MAX_BYTES)
    roots = allowed_roots(settings if settings is not None else load_settings())
    try:
        # _managed_path retains the HTTP route's existing relative precedence;
        # canonicalize immediately afterwards so the launcher never receives a
        # traversal or a symlink that resolves outside the managed roots.
        candidate = _managed_path(value, roots)
        path = candidate.resolve(strict=False)
    except FileListError as error:
        raise _ActionFailure(error.message, 403 if error.code == "forbidden" else 400) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise _ActionFailure("could not resolve path", 400) from error
    if not _inside(path, roots):
        raise _ActionFailure("path is outside the allowed repos", 403)
    if not path.exists():
        raise _ActionFailure("path does not exist", 200)
    if operation == "open" and not path.is_file():
        raise _ActionFailure("path must be an existing regular file", 200)
    if operation == "reveal" and not (path.is_file() or path.is_dir()):
        raise _ActionFailure("path must be an existing file or directory", 200)
    return path


def file_open_action(raw: Any, settings: Optional[dict] = None) -> Tuple[dict, int]:
    try:
        return _action_response(open_path(_action_path(raw, "open", settings))), 200
    except _ActionFailure as error:
        return _action_response(error.message), error.status
    except Exception as error:
        return _action_response(error), 200


def file_reveal_action(raw: Any, settings: Optional[dict] = None) -> Tuple[dict, int]:
    try:
        return _action_response(reveal_path(_action_path(raw, "reveal", settings))), 200
    except _ActionFailure as error:
        return _action_response(error.message), error.status
    except Exception as error:
        return _action_response(error), 200


def _validate_action_url(raw: Any) -> str:
    value = _validate_action_value(raw, "url", ACTION_URL_MAX_BYTES)
    try:
        parsed = urlsplit(value)
        # Accessing hostname and port performs urllib's strict authority
        # checks (including malformed brackets and out-of-range ports).
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as error:
        raise _ActionFailure("URL has a malformed port or authority", 400) from error
    if parsed.scheme.lower() not in ("http", "https"):
        raise _ActionFailure("only http(s) URLs can be opened", 400)
    if any(char.isspace() for char in value):
        raise _ActionFailure("URL must not contain whitespace", 400)
    if not hostname:
        raise _ActionFailure("URL must have a hostname", 400)
    if "@" in parsed.netloc:
        raise _ActionFailure("URL userinfo is not allowed", 400)
    # urllib treats a trailing colon as an absent port, but it is not a valid
    # authority for this action and is commonly an accidental malformed port.
    if parsed.netloc.endswith(":"):
        raise _ActionFailure("URL has a malformed port or authority", 400)
    return value


def url_open_action(raw: Any) -> Tuple[dict, int]:
    try:
        return _action_response(open_url(_validate_action_url(raw))), 200
    except _ActionFailure as error:
        return _action_response(error.message), error.status
    except Exception as error:
        return _action_response(error), 200


# Magic-byte signatures for the image formats silhouette-card-maker accepts
# (its `valid_mimetypes` list in utilities.py). The Workbench is stdlib-only,
# so this sniffs the file header instead of the `filetype` package — keeping
# "is this an image?" consistent with what create_pdf.py itself counts.
def is_image_file(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(16)
    except Exception:
        return False
    if len(head) < 4:
        return False
    if head[:3] == b"\xff\xd8\xff":                        # JPEG
        return True
    if head[:8] == b"\x89PNG\r\n\x1a\n":                # PNG / APNG
        return True
    if head[:4] == b"GIF8":                               # GIF
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":    # WebP
        return True
    if head[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):      # TIFF
        return True
    if head[:2] == b"BM":                                 # BMP
        return True
    if head[4:8] == b"ftyp" and head[8:12] in (b"av01", b"avif", b"heif", b"hevc", b"mif1"):
        return True                                       # AVIF / HEIF
    if head[:4] == b"qoif":                               # QOI
        return True
    if head[:8] == b"DDS <wal":                           # Direct3D surface
        return True
    if head[:12] == b"\x00\x00\x00\x0cJP\x20\x31\x31\x0a\x0d\x08":  # JP2 (JPEG 2000)
        return True
    return False


def allowed_roots(settings: dict) -> List[Path]:
    roots = [DATA_DIR, UI_DIR]
    for p in effective_dirs(settings):
        if p:
            roots.append(p)
    return [r for r in roots if r]


def _inside(path: Path, roots: List[Path]) -> bool:
    try:
        rp = path.resolve()
    except Exception:
        return False
    return any(rp == r or r in rp.parents for r in (x.resolve() for x in roots))


# Native artifact reads are deliberately bounded independently of the JSON-lines
# frame limit. A directory iterator is never materialized before the scan
# limit is applied.
FILE_LIST_MAX_SCANNED = 8192
FILE_LIST_MAX_ITEMS = 1024
FILE_LIST_MAX_RESULT_BYTES = 512 * 1024


class FileListError(Exception):
    """A safe, user-facing failure while resolving a managed directory."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _managed_path(raw: Path | str, roots: List[Path]) -> Path:
    """Resolve a managed path without following it outside the sandbox."""
    p = Path(raw)
    if not p.is_absolute():
        # Preserve the HTTP route's precedence for relative paths: the first
        # existing root wins, otherwise paths are relative to the first root.
        candidate = next((root / p for root in roots if (root / p).exists()), None)
        p = candidate or (roots[0] / p if roots else p)
    if not _inside(p, roots):
        raise FileListError("forbidden", "path is outside the allowed repos")
    return p


def list_files(path: Path | str, images_only: bool = False,
               settings: Optional[dict] = None) -> dict:
    """Return a bounded, read-only listing from the managed-path sandbox.

    ``scanned`` is the number of directory entries inspected and ``found`` is
    the number matching the filter during that scan. ``truncated`` is
    conservative when a bound is reached; callers must not use a truncated
    listing as the basis for a destructive follow-up.
    """
    roots = allowed_roots(settings if settings is not None else load_settings())
    p = _managed_path(path, roots)
    if not p.exists():
        raise FileListError("not_found", "path does not exist")
    if not p.is_dir():
        raise FileListError("not_directory", "path is not a directory")

    items = []
    scanned = 0
    found = 0
    truncated = False
    # Account for each encoded item once instead of serializing the growing
    # result for every entry. Reserve the maximum digit width for counters and
    # the larger boolean token so the final result remains within the bound.
    try:
        dir_encoded_size = len(json.dumps(str(p), ensure_ascii=False).encode("utf-8"))
    except (TypeError, UnicodeError, ValueError) as error:
        raise FileListError("unreadable", "could not encode directory listing") from error
    counter_width = len(str(max(FILE_LIST_MAX_SCANNED, FILE_LIST_MAX_ITEMS)))
    fixed_size = (len(b'{"dir":') + dir_encoded_size
                  + len(b',"exists":true,"items":[')
                  + len(b'],"truncated":false,"scanned":') + counter_width
                  + len(b',"found":') + counter_width + len(b'}'))
    item_bytes = 0
    try:
        iterator = iter(p.iterdir())
        for _ in range(FILE_LIST_MAX_SCANNED):
            try:
                child = next(iterator)
            except StopIteration:
                break
            scanned += 1
            # Never disclose an entry whose symlink resolves outside the
            # managed roots, even when the directory itself is safe.
            if not _inside(child, roots):
                truncated = True
                continue
            try:
                is_dir = child.is_dir()
                if images_only and (is_dir or not child.is_file() or not is_image_file(child)):
                    continue
                size = 0 if is_dir else child.stat().st_size
            except (OSError, ValueError):
                # The caller cannot safely treat an unreadable entry as absent,
                # especially when the listing gates a destructive follow-up.
                truncated = True
                continue
            found += 1
            if len(items) >= FILE_LIST_MAX_ITEMS:
                truncated = True
                continue
            item = {"name": child.name, "dir": is_dir, "size": size, "path": str(child)}
            try:
                encoded_item_size = len(json.dumps(
                    item, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8"))
            except (TypeError, UnicodeError, ValueError):
                truncated = True
                continue
            projected = fixed_size + item_bytes + encoded_item_size + (1 if items else 0)
            if projected > FILE_LIST_MAX_RESULT_BYTES:
                truncated = True
                break
            items.append(item)
            item_bytes += encoded_item_size + (1 if len(items) > 1 else 0)
    except (OSError, ValueError) as error:
        raise FileListError("unreadable", "could not read directory") from error

    items.sort(key=lambda item: item["name"])
    # Reaching the scan ceiling is reported as truncated even for exactly that
    # many entries: proving completeness requires one extra unbounded probe.
    if scanned >= FILE_LIST_MAX_SCANNED:
        truncated = True
    result = {"dir": str(p), "exists": True, "items": items,
              "truncated": bool(truncated), "scanned": scanned, "found": found}
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise FileListError("unreadable", "could not encode directory listing") from error
    if len(encoded) > FILE_LIST_MAX_RESULT_BYTES:
        # Keep the final guard for unusual filesystem changes or counter widths;
        # it is not part of the normal per-item accounting path.
        result["items"] = []
        result["truncated"] = True
    return result


# Descriptive alias retained for callers that used the HTTP helper name while
# the native contract identifies this operation as file.list.
list_managed_directory = list_files


def reveal_path(path: Path) -> Optional[str]:
    """Reveal a path in the platform file manager. Returns an error string or None."""
    if not path.exists():
        return "path does not exist"
    try:
        if os.name == "nt":
            if path.is_dir():
                subprocess.Popen(["explorer", str(path)], **_external_proc_kwargs())
            else:
                # Explorer's /select, action reveals the file without opening
                # it in whatever application is associated with its suffix.
                subprocess.Popen(["explorer", f"/select,{path}"],
                                 **_external_proc_kwargs())
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if not path.is_dir() else "", str(path)] if not path.is_dir()
                             else ["open", str(path)], **_external_proc_kwargs())
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)],
                             **_external_proc_kwargs())
        return None
    except Exception as e:
        return str(e)


def open_path(path: Path) -> Optional[str]:
    """Open a file in the platform's default application (double-click semantics).

    Unlike reveal_path this launches the file itself — e.g. a .studio3 cutting
    template opens in Silhouette Studio if it is installed. Returns an error
    string or None."""
    if not path.exists():
        return "path does not exist"
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], **_external_proc_kwargs())
        else:
            subprocess.Popen(["xdg-open", str(path)], **_external_proc_kwargs())
        return None
    except Exception as e:
        return str(e)


def open_url(url: str) -> Optional[str]:
    """Open a URL in the platform's default browser (the link twin of
    open_path). The UI's webview can't window.open, so its link buttons go
    through here instead. Returns an error string or None."""
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], **_external_proc_kwargs())
        else:
            subprocess.Popen(["xdg-open", url], **_external_proc_kwargs())
        return None
    except Exception as e:
        return str(e)


# ============================================================================
# HTTP layer
# ============================================================================

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
    ".dxf": "application/dxf",
    ".studio3": "application/octet-stream",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}


def resolve_template(paper: str, card: str, borderless: bool,
                     settings: Optional[dict] = None) -> dict:
    """Resolve one upstream cutting template for both HTTP and native IPC."""
    settings = settings if settings is not None else load_settings()
    paper = paper.strip().lower()
    card = card.strip().lower()
    if not (paper and card):
        return {"ok": False, "errors": ["needs both a card size and a paper size"]}

    # get_info remains authoritative for card ownership and source precedence;
    # this helper only locates the file selected by that upstream metadata.
    info = get_info()
    scm_root, extras_root = effective_dirs(settings)
    cards = {}
    for c in info.get("scm", {}).get("card_sizes", []):
        cards.setdefault(c["name"].lower(), c)
    for c in info.get("extras", {}).get("card_sizes", []):
        cards[c["name"].lower()] = c
    c = cards.get(card)
    if c is None:
        return {"ok": False, "errors": [f"no card size named “{card}” in either repo"]}

    sub = "borderless" if borderless else ""
    if c.get("source") == "extras":
        probes = [("scm-extras", extras_root and extras_root / "cutting_templates" / sub),
                  ("silhouette-card-maker", scm_root and scm_root / "cutting_templates" / sub)]
    else:
        probes = [("silhouette-card-maker", scm_root and scm_root / "cutting_templates" / sub),
                  ("scm-extras", extras_root and extras_root / "cutting_templates" / sub)]
    roots = allowed_roots(settings)
    fam = " (borderless)" if borderless else ""
    infix = "-borderless" if borderless else ""
    for repo, directory in probes:
        if not directory or not directory.is_dir() or not _inside(directory, roots):
            continue
        best, best_v = None, -1
        pat = re.compile(rf"^{re.escape(paper)}-{re.escape(card)}{re.escape(infix)}-v(\d+)\.studio3$")
        try:
            for candidate in directory.iterdir():
                # A matching symlink is not a safe template unless it resolves
                # within one of the managed roots.
                if not _inside(candidate, roots) or not candidate.is_file():
                    continue
                m = pat.fullmatch(candidate.name)
                if m and int(m.group(1)) > best_v:
                    best, best_v = candidate, int(m.group(1))
        except OSError:
            continue
        if best is not None:
            return {"ok": True, "name": best.name, "path": str(best), "repo": repo}
    return {"ok": False, "errors": [
        f"no cutting template for {paper} + {card}{fam} in either repo "
        f"(looked for {paper}-{card}{'-borderless' if borderless else ''}-v*.studio3)"]}


class Handler(BaseHTTPRequestHandler):
    server_version = f"SCMWorkbench/{SERVER_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[workbench] %s\n" % (fmt % args))

    # ---- plumbing ----

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: list = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        q = parse_qs(url.query)
        try:
            if path == "/smoke":
                # Build/CI proof that a real WKWebView can reach this server.
                # curl reaching /api/info proves the worker; this proves the
                # webview (the piece three consecutive releases shipped
                # broken because nothing in CI ever looked at it): the Tauri
                # shell points its window at this origin, and this endpoint
                # answers only a connection the webview itself opens.
                return self._json({
                    "smoke": "ok",
                    "version": SERVER_VERSION,
                    "client": self.headers.get("User-Agent", ""),
                })
            if path in ("/", "/index.html"):
                ua = self.headers.get("User-Agent", "")
                if "AppleWebKit" in ua:
                    # A WebKit session on the index: the worker says so at
                    # request time. This is the marker the CI smoke test
                    # greps for, and on a real machine it is the answer to
                    # "is the window on the UI?" - the app's own webview,
                    # not curl.
                    sys.stderr.write("[workbench] webview session on / (WebKit)\n")
                return self._static("index.html")
            if path.startswith("/ui/"):
                return self._static(path[4:])
            if path == "/favicon.svg":
                return self._static("favicon.svg")
            if path == "/up":
                # Liveness probe: the app window's “starting” page polls this
                # until the server is ready, then navigates to the real UI. The
                # permissive CORS header keeps that fetch reliable in WKWebView.
                return self._send(200, _GIF_1PX, ctype="image/gif",
                                  extra=[("Access-Control-Allow-Origin", "*")])
            if path == "/api/info":
                return self._json(get_info())
            if path == "/api/release-notes":
                return self._json(release_notes_view())
            if path == "/api/repos":
                return self._json({"repos": repos_view(load_settings())})
            if path == "/api/manifest":
                return self._json(get_manifest())
            if path == "/api/jobs":
                return self._json(list_jobs())
            m = re.fullmatch(r"/api/jobs/([\w-]+)/log", path)
            if m:
                jid = m.group(1)
                if not (_job_record(jid) or _persisted_record(jid)):
                    return self._json({"error": "job not found"}, 404)
                try:
                    after = int((q.get("after") or ["0"])[0])
                    max_lines = int((q.get("max_lines") or [str(JOB_LOG_MAX_LINES)])[0])
                    if after < 0 or not (1 <= max_lines <= JOB_LOG_MAX_LINES):
                        raise ValueError
                except (TypeError, ValueError):
                    return self._json({"error": "invalid log cursor"}, 400)
                return self._json(get_job_log(jid, after, max_lines))
            m = re.fullmatch(r"/api/jobs/([\w-]+)/stream", path)
            if m:
                after = int((q.get("after") or ["0"])[0])
                return self._sse(m.group(1), after)
            if path == "/api/file":
                return self._file(q)
            if path == "/api/template":
                return self._template(q)
            if path == "/api/preview":
                return self._preview(q)
            if path == "/api/settings":
                return self._json(load_settings())
            if path == "/api/updates":
                return self._json({
                    "current": SERVER_VERSION,
                    "repo": updater.UPDATE_REPO,
                    "packaged": os.environ.get("SCM_WORKBENCH_PACKAGED") == "1",
                    "bundle": os.environ.get("SCM_WORKBENCH_BUNDLE") or "",
                    "state": load_update_state(),
                })
            if path.startswith("/api/"):
                return self._json({"error": f"no such route: {path}"}, 404)
            # SPA routes (/pdf, /settings, ...): serve the app shell and let
            # the client pick the page from the URL, so a refresh stays put.
            return self._static("index.html")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    # ---- POST ----

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/api/jobs":
                body = self._body()
                job, errors = start_job(str(body.get("kind", "")), body.get("args") or {})
                if errors:
                    return self._json({"ok": False, "errors": errors}, 400)
                return self._json({"ok": True, "job": {
                    "id": job["id"], "title": job["title"], "status": job["status"],
                    "cmd": job["cmd"], "warnings": job.get("warnings", []),
                }})
            m = re.fullmatch(r"/api/jobs/([\w-]+)/kill", path)
            if m:
                return self._json({"ok": kill_job(m.group(1))})
            if path == "/api/updates/check":
                body = self._body()
                force = bool(body.get("force"))
                if not force:
                    st = load_update_state()
                    # recently checked and nothing pending → the check is a no-op
                    if st.get("status") in ("up-to-date", "update-available") and st.get("checked_at") is not None:
                        try:
                            age = time.time() - float(st["checked_at"])
                        except Exception:
                            age = None
                        if age is not None and age < UPDATE_CHECK_INTERVAL:
                            st["cached"] = True
                            return self._json({"ok": True, "state": st})
                return self._json({"ok": True, "state": run_update_check()})
            if path == "/api/updates/start":
                body = self._body()
                job, errors = start_update_job(str(body.get("latest") or ""), bool(body.get("force")))
                if errors:
                    return self._json({"ok": False, "errors": errors}, 400)
                return self._json({"ok": True, "job": {
                    "id": job["id"], "title": job["title"], "status": job["status"],
                    "cmd": job["cmd"],
                }})
            if path == "/api/settings":
                # HTTP keeps its historical direct-patch body shape, while the
                # native method wraps the same patch as {changes: ...}.  Invalid
                # patches are application results with HTTP 400, matching the
                # native result exactly; no invalid patch reaches save_settings.
                body = self._body()
                result = update_settings(body)
                return self._json(result, 200 if result.get("ok") else 400)
            if path == "/api/repos/save":
                body = self._body()
                key = str(body.get("repo") or "")
                if key not in repo_sync.REPOS:
                    return self._json({"ok": False, "errors": [f"unknown repo “{key}”"]}, 400)
                source = str(body.get("source") or "").strip()
                if not source:
                    return self._json({"ok": False, "errors": ["no source given"]}, 400)
                # validate that the ref actually resolves before persisting it
                try:
                    target = repo_sync.resolve_target(key, source)
                except repo_sync.RepoError as e:
                    return self._json({"ok": False, "errors": [str(e)]}, 400)
                # This endpoint remains the sole writer for the excluded
                # `repos` settings.  Keep its read-modify-write under the same
                # lock as settings.set so concurrent updates cannot be lost.
                with _SETTINGS_LOCK:
                    settings = load_settings()
                    settings.setdefault("repos", {}).setdefault(
                        key, {"source": repo_sync.REPOS[key].get("default_source", "main")})
                    settings["repos"][key]["source"] = source if source in ("main", "latest-release") else "pinned"
                    settings["repos"][key]["pin"] = source if source not in ("main", "latest-release") else ""
                    save_settings(settings)
                repo_sync.set_source(key, source)
                # record a check immediately — we already know the target, so the UI
                # can show “new version available” without a second round-trip
                state = repo_sync.load_state()
                r = state.setdefault(key, {})
                deployed = r.get("deployed")
                r["last_check"] = {"checked": {"repo": key, "ok": True, "cached": False, "target": target,
                                                 "deployed": deployed,
                                                 "up_to_date": bool(deployed and deployed.get("sha") == target["sha"])},
                                  "checked_at": time.time()}
                repo_sync.save_state(state)
                return self._json({"ok": True, "repo": key, "source": source, "target": target,
                                  "repos": repos_view(settings)})
            if path == "/api/repos/check":
                body = self._body()
                key = str(body.get("repo") or "")
                if key not in repo_sync.REPOS:
                    return self._json({"ok": False, "errors": [f"unknown repo “{key}”"]}, 400)
                res = run_repo_check(key, force=bool(body.get("force")))
                if res.get("ok"):
                    res["last_check"] = repo_sync.load_state().get(key, {}).get("last_check")
                return self._json({"ok": res.get("ok", False), **(res if res.get("ok") else {"errors": [res.get("error", "check failed")]}),
                                   "repos": repos_view(load_settings())})
            if path == "/api/repos/refs":
                body = self._body()
                key = str(body.get("repo") or "")
                if key not in repo_sync.REPOS:
                    return self._json({"ok": False, "errors": [f"unknown repo “{key}”"]}, 400)
                try:
                    cached = _refs_cache.get(key)
                    if cached and time.time() - cached[0] < 3600:
                        refs = cached[1]
                    else:
                        refs = repo_sync.list_refs(key)
                        _refs_cache[key] = (time.time(), refs)
                except repo_sync.RepoError as e:
                    return self._json({"ok": False, "errors": [str(e)]}, 400)
                return self._json({"ok": True, "repo": key, "refs": refs})
            if path == "/api/decklists/import":
                body = self._body()
                src = str(body.get("path") or "").strip()
                if not src or not os.path.isfile(src):
                    return self._json({"ok": False, "errors": [f"no such file: “{src or '(none given)'}”"]}, 400)
                scm, _ = effective_dirs(load_settings())
                if not scm:
                    return self._json({"ok": False, "errors": ["no copy of silhouette-card-maker is connected yet"]}, 400)
                dl = scm / "game" / "decklist"
                dl.mkdir(parents=True, exist_ok=True)
                name = os.path.basename(src)
                target = dl / name
                n = 2
                while target.exists():
                    stem, ext = os.path.splitext(name)
                    target = dl / f"{stem} ({n}){ext}"
                    n += 1
                try:
                    shutil.copy2(src, target)
                except Exception as e:
                    return self._json({"ok": False, "errors": [f"copy failed: {e}"]}, 400)
                invalidate_manifest_cache()
                placeholders = {"README.md", "EMPTY.md"}
                decklists = [
                    {"name": pp.name, "size": pp.stat().st_size}
                    for pp in sorted(dl.iterdir()) if pp.is_file() and pp.name not in placeholders
                ]
                return self._json({"ok": True, "name": target.name, "decklists": decklists})
            if path == "/api/offset":
                body = self._body()
                size = str(body.get("size") or "").strip()
                try:
                    x = int(float(body.get("x", 0) or 0))
                    y = int(float(body.get("y", 0) or 0))
                    angle = float(float(body.get("angle", 0) or 0))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "errors": ["offset values must be numbers"]}, 400)
                settings = load_settings()
                scm, _ = effective_dirs(settings)
                if not scm:
                    return self._json({"ok": False, "errors": ["SCM repo not found — set it in Settings."]}, 400)
                if body.get("delete"):
                    if not size:
                        return self._json({"ok": False, "errors": ["no paper size to remove"]}, 400)
                    table = load_per_size_offsets()
                    if size in table:
                        del table[size]
                        save_per_size_offsets(table)
                    return self._json({"ok": True, "removed": size})
                if size:
                    # per-paper-size row: store it in the Workbench's own table,
                    # then stage it into SCM's shared file so plain --load_offset
                    # / saved-offset runs pick it up without any SCM-side change.
                    table = load_per_size_offsets()
                    table[size] = {"x": x, "y": y, "angle": angle}
                    save_per_size_offsets(table)
                    write_global_offset(scm, x, y, angle)
                    return self._json({"ok": True, "size": size, "staged": True,
                                       "offset": {"x_offset": x, "y_offset": y, "angle_offset": angle}})
                write_global_offset(scm, x, y, angle)
                return self._json({"ok": True, "offset": {"x_offset": x, "y_offset": y, "angle_offset": angle}})
            if path == "/api/files/save":
                body = self._body()
                src = os.path.expanduser(str(body.get("src") or "").strip())
                dest = os.path.expanduser(str(body.get("dest") or "").strip())
                errors = []
                if not src or not os.path.isfile(src):
                    errors.append("The file to move doesn't exist (yet).")
                if not dest:
                    errors.append("No destination chosen.")
                if errors:
                    return self._json({"ok": False, "errors": errors}, 400)
                # the source must live in a workbench-managed location; the
                # destination is anywhere the user pointed the save panel at.
                try:
                    if not _inside(Path(src), allowed_roots(load_settings())):
                        return self._json({"ok": False, "errors": ["that file isn't in a workbench-managed location"]}, 403)
                except Exception:
                    pass
                dest_dir = os.path.dirname(dest)
                if dest_dir:
                    os.makedirs(dest_dir, exist_ok=True)
                final, n = dest, 2
                base, ext = os.path.splitext(dest)
                while os.path.exists(final):
                    final = f"{base} ({n}){ext}"
                    n += 1
                try:
                    shutil.copy2(src, final)
                    return self._json({"ok": True, "dest": final, "name": os.path.basename(final)})
                except Exception as e:
                    return self._json({"ok": False, "errors": [f"Could not copy the file: {e}"]}, 500)
            if path == "/api/reveal":
                body = self._body()
                result, status = file_reveal_action(body.get("path"), load_settings())
                return self._json(result, status)
            if path == "/api/fs":
                body = self._body()
                raw = str(body.get("path") or "").strip()
                if not raw:
                    return self._json({"ok": False, "errors": ["no path"]}, 400)
                p = Path(raw)
                roots = allowed_roots(load_settings())
                if not p.is_absolute():
                    cand = next((r / p for r in roots if (r / p).exists()), None)
                    p = cand or (roots[0] / p if roots else p)
                if not _inside(p, roots):
                    return self._json({"ok": False, "errors": ["path is outside the allowed repos"]}, 403)
                if body.get("op") == "delete_images":
                    if not p.is_dir():
                        return self._json({"ok": True, "deleted": 0, "names": [], "dir": str(p)})
                    names = []
                    for f in sorted(p.iterdir()):
                        if not f.is_file() or not is_image_file(f):
                            continue  # non-images (READMEs, EMPTY.md, …) are left alone
                        try:
                            f.unlink()
                            names.append(f.name)
                        except Exception as e:
                            return self._json({"ok": False, "errors": [f"could not delete {f.name}: {e}"],
                                                "deleted": len(names)}, 400)
                    return self._json({"ok": True, "deleted": len(names), "names": names, "dir": str(p)})
                return self._json({"ok": False, "errors": [f"unknown op \u201c{body.get('op')}\u201d"]}, 400)
            return self._json({"error": f"no such route: {path}"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    # ---- handlers ----

    def _static(self, rel: str):
        p = (UI_DIR / rel).resolve()
        if not _inside(p, [UI_DIR]):
            return self._json({"error": "forbidden"}, 403)
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        ctype = MIME.get(p.suffix.lower(), "application/octet-stream")
        if rel == "index.html":
            # Version the asset URLs (?v=<mtime>): the app window's webview keeps a
            # persistent URL cache, and bare /ui/... paths can hand a stale
            # theme.css/app.js to a window long after a redeploy. A new mtime on
            # deploy = a new URL = a guaranteed cache miss, so any window that
            # (re)loads after an update is guaranteed to see the new UI.
            def _v(name: str) -> str:
                try:
                    return str(int((UI_DIR / name).stat().st_mtime))
                except Exception:
                    return "0"
            text = data.decode("utf-8")
            for name in ("theme.css", "js/app.js", "favicon.svg"):
                text = text.replace(f"/ui/{name}", f"/ui/{name}?v={_v(name)}")
            data = text.encode("utf-8")
        self._send(200, data, ctype)

    def _file(self, q):
        url = (q.get("url") or [""])[0]
        if url:
            # Same open semantics as open=1, aimed at a link: the server
            # opens it in the default browser. Validation is shared with the
            # native RPC action.
            result, status = url_open_action(url)
            return self._json(result, status)
        settings = load_settings()
        rel = (q.get("path") or [""])[0]
        reveal = (q.get("reveal") or ["0"])[0] == "1"
        do_open = (q.get("open") or ["0"])[0] == "1"
        if not rel:
            return self._json({"error": "no path"}, 400)
        # Action requests use the same canonical resolver as native RPC. Keep
        # this ahead of the legacy raw-read path so action errors retain the
        # established {ok, errors} shape instead of the read route's {error}.
        if do_open:
            result, status = file_open_action(rel, settings)
            return self._json(result, status)
        if reveal:
            result, status = file_reveal_action(rel, settings)
            return self._json(result, status)

        roots = allowed_roots(settings)
        p = Path(rel)
        if not p.is_absolute():
            cand = next((r / rel for r in roots if (r / rel).exists()), None)
            p = cand or (roots[0] / rel if roots else rel)
        if not _inside(p, roots):
            return self._json({"error": "path outside sandbox"}, 403)
        if p.is_dir():
            try:
                listing = list_files(
                    p, (q.get("images_only") or [""])[0] == "1", settings)
            except FileListError as error:
                status = 403 if error.code == "forbidden" else 404
                return self._json({"error": "path outside sandbox" if status == 403 else "not found"}, status)
            return self._json(listing)
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        self._send(200, data, MIME.get(p.suffix.lower(), "application/octet-stream"),
                   [("Content-Disposition", f'inline; filename="{p.name}"')])

    def _template(self, q):
        """Resolve a cutting template using the shared HTTP/native helper."""
        settings = load_settings()
        paper = (q.get("paper") or [""])[0]
        card = (q.get("card") or [""])[0]
        borderless = (q.get("borderless") or ["0"])[0] == "1"
        return self._json(resolve_template(paper, card, borderless, settings))

    def _preview(self, q):
        kind = (q.get("kind") or [""])[0]
        args_json = (q.get("args") or ["{}"])[0]
        # A wiped form slot serializes as the literal string "undefined" from
        # a stale client; treat that and the old literal "null" as defaults.
        if args_json in ("undefined", "null"):
            args_json = "{}"
        try:
            args = json.loads(args_json)
        except Exception:
            return self._json({"error": "bad args"}, 400)
        try:
            result = build_preview(kind, args)
        except PreviewError as error:
            return self._json(error.http_body, error.status)
        return self._json(result)

    def _sse(self, jid: str, after: int):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for event, data in sse_stream(jid, after):
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ============================================================================
# Entry point
# ============================================================================

def _in_wsl() -> bool:
    import os
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _wsl_vm_ip():
    """Best-effort IP address of this WSL2 VM as seen from the Windows host.

    In the default NAT networking mode this is the 172.x address WSL hands
    the VM; traffic from the host to it is direct (no per-connection
    localhost proxying, which is the flaky part). Returns None when it
    can't be determined (e.g. mirrored networking mode).
    """
    import ipaddress
    import subprocess

    def clean(cand):
        try:
            ip = ipaddress.ip_address(cand)
            if not ip.is_loopback and not ip.is_link_local:
                return str(ip)
        except (ValueError, TypeError):
            pass
        return None

    for cmd in (["ip", "-4", "route", "get", "1.1.1.1"], ["hostname", "-I"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        tokens = []
        if cmd[0] == "ip":
            # "1.1.1.1 via 172.28.160.1 dev eth0 src 172.28.160.100"
            if "src" in out:
                tokens = [out.rsplit("src", 1)[1].strip().split()[0]]
        else:
            tokens = out.split()
        for t in tokens:
            if c := clean(t):
                return c
    return None


def _open_browser(url: str) -> None:
    """Open the Workbench URL in the user's browser, quietly.

    The stdlib webbrowser hands the URL to an OS launcher (xdg-open, gio,
    open, ...) whose child inherits this server's stderr — so a machine
    with no web app configured (the usual WSL2 case) spits raw output
    like “gio: <url>: Operation not supported” into the console. We run
    the candidate launchers ourselves with output silenced instead: the
    first one that succeeds wins, and if none does we print one friendly
    line.
    """
    import os
    import shlex
    import subprocess

    # Small helpers whose exit code is trustworthy: 0 = a handler was
    # launched, non-zero = nothing was opened.
    reliable = {"xdg-open", "gio", "gvfs-open", "x-www-browser", "kfmclient", "kfm", "open"}

    def candidates():
        env_browser = (os.environ.get("BROWSER") or "").strip()
        if env_browser:
            try:
                parts = shlex.split(env_browser)
                if parts:
                    yield [p.replace("%s", url).replace("%u", url) for p in parts]
            except ValueError:
                pass
        if _in_wsl():
            # Windows-side launchers only: the user's browser lives in
            # Windows. (The Linux-side xdg-open/gio would just produce the
            # “no default web app” noise in a WSL session.)
            yield ["explorer.exe", url]
            yield ["cmd.exe", "/c", "start", "", url]
        elif sys.platform == "darwin":
            yield ["open", url]
        elif os.name != "nt":
            yield ["xdg-open", url]
            yield ["gio", "open", "--", url]

    for cmd in candidates():
        name = os.path.basename(cmd[0])
        try:
            if name in reliable:
                try:
                    rc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode
                except subprocess.TimeoutExpired:
                    return  # it may have forked a browser before hanging — don't fire another
                if rc == 0:
                    return
                # non-zero: this helper genuinely opened nothing — next candidate is safe
            else:
                # GUI shells (explorer.exe, cmd /c start, a browser binary
                # from $BROWSER) may open the browser and still exit
                # non-zero or linger, so waiting on them and retrying the
                # next candidate opens a *second* browser. A successful
                # spawn is success: leave the process and stop.
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except (FileNotFoundError, PermissionError):
            continue  # not present on this platform

    if os.name == "nt" and not _in_wsl():
        try:
            if webbrowser.open(url, new=2):
                return
        except Exception:
            pass
    _diag(f"  (Could not open a browser automatically — visit {url} manually.)")


class WorkbenchHTTPServer(ThreadingHTTPServer):
    """HTTP server that owns the same upstream children as the IPC server."""
    def server_close(self):
        stop_all_jobs()
        super().server_close()


def start_http(host: str, port: int) -> ThreadingHTTPServer:
    """Bind the UI server (no serve_forever — the caller runs it).

    Port 0 lets the OS pick a free one (window mode, where the settings
    port may be taken by something else).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        srv = WorkbenchHTTPServer((host, port), Handler)
    except OSError as e:
        # The one bind failure that matters, stated plainly: another process
        # already owns the port — usually a previous app instance whose window
        # was killed without a clean close (the app tries to reclaim such
        # ports itself at start; this is the last line of defence).
        _diag(
            f"\n  [server] could not bind {host}:{port} — another process already holds that port ({e}).\n"
            f"         Close the other SCM Workbench (or whatever else uses port {port}) and try again.\n",
        )
        sys.exit(1)
    srv.daemon_threads = True
    return srv


def main():
    global _IPC_MODE
    ap = argparse.ArgumentParser(description="SCM Workbench — local UI for silhouette-card-maker + scm-extras")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None,
                    help="Address to bind (default: 127.0.0.1; on WSL all interfaces of the VM, "
                         "so the Windows host can also reach the server)")
    ap.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    ap.add_argument("--ipc", action="store_true",
                    help="Also serve the native newline-delimited JSON child protocol")
    args = ap.parse_args()
    _IPC_MODE = bool(args.ipc)

    # Make the banner (and any traceback) robust on *any* stream: a freshly
    # spawned Windows child defaults its stdio to the machine's ANSI codepage
    # (e.g. cp1252), which cannot encode the box-drawing characters in the
    # banner below — the write used to raise UnicodeEncodeError before the port
    # was ever bound, so the UI never appeared. `errors="replace"` means a
    # hostile codepage can never kill the server at startup; the worst case is
    # a couple of '?' glyphs where a fancy character used to be.
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass

    settings = load_settings()
    port = args.port if args.port is not None else int(settings.get("port") or DEFAULT_PORT)
    scm, extras = effective_dirs(settings)

    host = args.host
    if host is None:
        # WSL2 NAT networking: bind all interfaces of the VM. The WSL
        # virtual network is only reachable from the Windows host, so this
        # is still "local only" — and now both 127.0.0.1 (from inside WSL)
        # and the VM's own address (from a Windows browser) work. Every other
        # platform keeps the loopback-only default.
        host = "0.0.0.0" if _in_wsl() else "127.0.0.1"

    # Bind before emitting the banner so --port 0 can report the actual port.
    server = start_http(host, port)
    actual_port = server.server_address[1]

    out = io.StringIO()
    w = out.write
    w("\n")
    w("  \x1b[1;1mSCM Workbench\x1b[0m  v%s\n" % SERVER_VERSION)
    w("  ───────────────────────────────────────────────────────\n")
    w("  SCM repo:      %s\n" % (scm if scm else "\x1b[31mnot found — point Settings at it\x1b[0m"))
    w("  Extras repo:   %s\n" % (extras if extras else "\x1b[33mnot found (optional)\x1b[0m"))
    w("  Python:        %s\n" % sys.version.split()[0])
    w("  ───────────────────────────────────────────────────────\n")
    url = f"http://{('127.0.0.1' if host == '0.0.0.0' else host)}:{actual_port}"
    w(f"  UI:  {url}\n")
    browser_url = url
    if _in_wsl():
        vm_ip = _wsl_vm_ip()
        if vm_ip:
            browser_url = f"http://{vm_ip}:{actual_port}"
            w(f"  Windows host:  {browser_url}  (your default browser opens here)\n")
        else:
            w("  Windows host:  use the 127.0.0.1 URL above (mirrored networking mode)\n")
    if _in_wsl():
        w("\n  Binds the WSL VM — reachable from the Windows host "
          "(and from your LAN only in mirrored networking mode). Ctrl+C to stop.\n")
    else:
        w("\n  Local only — not exposed to your network. Ctrl+C to stop.\n")
    _diag(out.getvalue())

    ipc_eof = None
    if args.ipc:
        # Import after the HTTP server is bound: this is one supervised child,
        # with the compatibility HTTP transport and native transport sharing
        # the same authoritative server functions. EOF owns shutdown as well
        # as merely waking the request loop, so no upstream child survives a
        # native shell disappearing.
        from scm_workbench import ipc
        ipc_eof = threading.Event()
        def _ipc_eof() -> None:
            stop_all_jobs()
            ipc_eof.set()
        ipc.start_thread(on_eof=_ipc_eof)

    # Record this server's pid so a future launcher can spot (and stop) an
    # orphaned UI server left over from a previous launch.
    try:
        (DATA_DIR / "server.pid").write_text(str(os.getpid()), encoding="ascii")
    except Exception:
        pass

    # at start-up (and then once a day) — quietly check for a newer release
    threading.Thread(target=_update_daemon, daemon=True, name="updater").start()

    # Packaged app, first boot: the app fetches its own managed repo copies
    # (the UI is already up — the dashboard banner shows the live progress
    # from bootstrap.json; see bootstrap.run_first_boot). Dev checkouts keep
    # the classic behavior: no automatic cloning, Settings drives it.
    if os.environ.get("SCM_WORKBENCH_PACKAGED") == "1" and not os.environ.get("SCM_WORKBENCH_NO_BOOTSTRAP"):
        from scm_workbench import bootstrap as _first_boot

        def _first_boot_then() -> None:
            _first_boot.run_first_boot(
                DATA_DIR,
                log=lambda message="": _diag(message),
            )
            # The manifest cache was built at startup from whatever the repos
            # held then (nothing, on a true first boot), and the mtime signal
            # it uses can be older than every write the bootstrap just made —
            # so rebuild it explicitly now the clones are in place.
            invalidate_manifest_cache()

        threading.Thread(
            target=_first_boot_then, daemon=True, name="first-boot",
        ).start()

    if not args.ipc and not args.no_browser and settings.get("auto_open_browser", True):
        threading.Timer(0.4, _open_browser, args=(browser_url,)).start()

    try:
        if ipc_eof is None:
            # Preserve the ordinary HTTP server path exactly.
            server.serve_forever()
        else:
            # A short timeout keeps EOF responsive while retaining the normal
            # ThreadingHTTPServer handler behavior, including SSE requests.
            server.timeout = 0.2
            while not ipc_eof.is_set():
                server.handle_request()
    except KeyboardInterrupt:
        _diag("\nBye.")
    finally:
        stop_all_jobs()
        server.server_close()


if __name__ == "__main__":
    main()
