#!/usr/bin/env python3
"""
SCM Workbench — a local web UI for silhouette-card-maker and scm-extras.

Zero dependencies beyond the Python standard library. Run:

    python server.py                  # starts on http://127.0.0.1:8037
    python server.py --port 9000     # different port
    python server.py --no-browser    # don't auto-open a browser tab

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
import os
import re
import shlex
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
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, Optional, Tuple

SERVER_VERSION = "1.0.0"
DEFAULT_PORT = 8037

UI_DIR = Path(__file__).resolve().parent / "ui"
DATA_DIR = Path(__file__).resolve().parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
JOBS_FILE = DATA_DIR / "jobs.json"
LOGS_DIR = DATA_DIR / "logs"

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
    for base in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        p = base / name
        if (p / marker).is_file():
            return p
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
                    _opt("only_fronts", "Front pages only", "toggle", default=False, width="third"),
                ],
            },
            {
                "title": "Card, paper & registration",
                "options": [
                    _opt("card_size", "Card size", "select", choices=card_choices, default="standard", width="third"),
                    _opt("paper_size", "Paper size", "select", choices=paper_choices, default="letter", width="third"),
                    _opt("registration", "Registration marks", "segment",
                         choices=[["3", "3 marks"], ["4", "4 marks"]], default="3", width="third"),
                    _opt("specialty", "Specialty layout", "select", choices=specialty_choices, default="", width="third",
                         help="Overrides card size, paper size, and registration."),
                    _opt("registration_orientation", "Registration orientation", "select",
                         choices=[["", "Auto (follow layout)"], ["portrait", "Portrait"], ["landscape", "Landscape"]],
                         default="", width="third"),
                    _opt("borderless", "Borderless (tighter inset)", "toggle", default=False, width="third",
                         help="Fits more cards per page by using a smaller inset."),
                ],
            },
            {
                "title": "Quality",
                "options": [
                    _opt("ppi", "Resolution (PPI)", "range", default=300, min=150, max=600, step=10, width="third"),
                    _opt("quality", "Compression quality", "range", default=100, min=0, max=100, step=1, width="third"),
                    _opt("load_offset", "Apply saved offset", "toggle", default=False, width="third",
                         help="Applies the saved X / Y / angle printer offset."),
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
                    _opt("x_offset", "X offset (px, right +)", "number", default="", width="quarter"),
                    _opt("y_offset", "Y offset (px, up +)", "number", default="", width="quarter"),
                    _opt("angle", "Angle (deg, clockwise +)", "number", step=0.1, default="", width="quarter"),
                    _opt("ppi", "PPI", "range", default=300, min=150, max=600, step=10, width="quarter"),
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
        "description": "Deletes every image in game/front/ and game/double_sided/ so you can start a new game fresh. The card back folder is left untouched.",
        "groups": [],
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
            "title": f"Fetch {meta['title']} card art",
            "page": "fetch", "needs": ["scm"], "cwd": "scm", "slug": slug,
            "description": f"Downloads card images for {meta['title']} from a decklist into the game/ folders.",
            "groups": fetch_groups(slug),
        }

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
    "auto_open_browser": True,
    "onboarded": False,
    "defaults": {
        "card_size": "standard",
        "paper_size": "letter",
        "ppi": 300,
        "quality": 100,
    },
}


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    data = _try_read_json(SETTINGS_FILE)
    if data:
        for k, v in data.items():
            if k == "defaults" and isinstance(v, dict):
                s["defaults"].update(v)
            else:
                s[k] = v
    return s


def save_settings(s: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def effective_dirs(settings: dict) -> Tuple[Optional[Path], Optional[Path]]:
    def resolve(p: str) -> Optional[Path]:
        if not p:
            return None
        pp = Path(p)
        if not pp.is_absolute():
            pp = Path(__file__).resolve().parent / pp
        return pp if pp.is_dir() else None

    scm = resolve(settings.get("scm_dir") or "") or find_scm_repo()
    extras = resolve(settings.get("extras_dir") or "") or find_extras_repo()
    return scm, extras


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
            "data_dir": str(DATA_DIR),
        },
        "scm": read_scm_info(scm, extras),
        "extras": read_extras_info(extras),
        "settings": settings,
    }


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


def _persist_jobs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        rows = sorted(JOBS.values(), key=lambda j: j["ts"], reverse=True)[:100]
    slim = [
        {k: j[k] for k in ("id", "ts", "kind", "title", "cmd", "status", "exit_code", "log_file", "duration")}
        for j in rows if j["status"] != "running" and j.get("duration") is not None
    ]
    try:
        with open(JOBS_FILE, "w", encoding="utf-8") as f:
            json.dump(slim, f, indent=1)
    except Exception:
        pass


def read_persisted_jobs() -> list:
    data = _try_read_json(JOBS_FILE)
    return data or []


def _utf8_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _proc_kwargs() -> dict:
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _fmt_argv(argv: List[str]) -> str:
    return " ".join(shlex.quote(p) if " " in p else p for p in argv)


def build_command(kind: str, args: dict, settings: dict, info: dict, write_deck: bool = True) -> Tuple[list, Optional[Path], dict, str, list, list]:
    """Assemble (argv, cwd, env, title, warnings, errors) for a job kind.

    The browser sends structured values only; argv is assembled here, in one
    place, which keeps command previews and real runs identical.
    """
    warnings: List[str] = []
    errors: List[str] = []
    scm, extras = effective_dirs(settings)
    python = Path(sys.executable)
    if settings.get("python"):
        p = Path(settings["python"])
        p = p if p.is_absolute() else Path(__file__).resolve().parent / p
        if p.exists():
            python = p
        else:
            warnings.append(f"Configured python not found ({p}); using {python.name}.")

    manifest = get_manifest()
    title = manifest.get(kind, {}).get("title", kind)
    env = _utf8_env()
    argv = [str(python)]

    def require_repo(name: str, path: Optional[Path], hint: str = "") -> bool:
        if path is None or not Path(path).is_dir():
            errors.append(f"{name} repo not found — set its path in Settings{'. ' + hint if hint else '.'}")
            return False
        return True

    d = settings.get("defaults", {})

    if kind == "create_pdf":
        if not require_repo("SCM", scm, "e.g. the silhouette-card-maker folder."):
            return argv, None, env, title, warnings, errors
        cwd = scm
        a = args
        argv += ["create_pdf.py"]
        if a.get("front_dir"): argv += ["--front_dir_path", str(a["front_dir"])]
        if a.get("back_dir"): argv += ["--back_dir_path", str(a["back_dir"])]
        if a.get("double_sided_dir"): argv += ["--double_sided_dir_path", str(a["double_sided_dir"])]
        argv += ["--output_path", str(a.get("output_path") or "game/output/game.pdf")]
        if a.get("output_images"): argv += ["--output_images"]
        card = str(a.get("card_size") or d.get("card_size") or "standard")
        paper = str(a.get("paper_size") or d.get("paper_size") or "letter")
        argv += ["--card_size", card, "--paper_size", paper]
        if a.get("registration"): argv += ["--registration", str(a["registration"])]
        if a.get("registration_orientation"): argv += ["--registration_orientation", str(a["registration_orientation"])]
        if a.get("specialty"): argv += ["--specialty", str(a["specialty"])]
        if a.get("only_fronts"): argv += ["--only_fronts"]
        if a.get("fit"): argv += ["--fit", str(a["fit"])]
        if a.get("fit_backs"): argv += ["--fit_backs", str(a["fit_backs"])]
        for key in ("crop", "crop_backs", "extend_edges", "extend_edges_backs",
                    "extend_corners", "extend_corners_backs", "extend_bleed", "extend_bleed_backs"):
            if a.get(key): argv += ["--" + key, str(a[key])]
        ppi = a.get("ppi")
        ppi = int(ppi) if ppi not in (None, "") else int(d.get("ppi", 300))
        quality = a.get("quality")
        quality = int(quality) if quality not in (None, "") else int(d.get("quality", 100))
        argv += ["--ppi", str(ppi), "--quality", str(quality)]
        for idx in a.get("skip") or []:
            argv += ["--skip", str(idx)]
        if a.get("label"): argv += ["--label", str(a["label"])]
        if a.get("show_outline"): argv += ["--show_outline"]
        if a.get("borderless"): argv += ["--borderless"]
        if a.get("load_offset"): argv += ["--load_offset"]
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
        argv += ["--ppi", str(int(a.get("ppi") or 300))]
        if a.get("save"):
            argv += ["-s"]

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
        argv += ["clean_up.py"]

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
            deck = str(a.get("deck_file") or "")
            if not deck:
                errors.append("Pick an existing decklist file (or use paste / URL).")
            elif is_url_format:
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

    else:
        errors.append(f"Unknown job kind: {kind}")
        return argv, None, env, title, warnings, errors

    return argv, cwd, env, title, warnings, errors


MANIFEST_CACHE: Dict[str, dict] = {}
MANIFEST_LOCK = threading.Lock()


def get_manifest() -> dict:
    with MANIFEST_LOCK:
        if not MANIFEST_CACHE:
            MANIFEST_CACHE.update(build_manifest(get_info()))
    return MANIFEST_CACHE


def invalidate_manifest_cache() -> None:
    with MANIFEST_LOCK:
        MANIFEST_CACHE.clear()


def normalize_args(spec: dict, raw: dict) -> Tuple[dict, List[str]]:
    """Coerce/validate raw client values against the manifest. Returns (args, errors)."""
    errors: List[str] = []
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
                    f = max(lo, min(hi, f))
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
    return args, errors


def start_job(kind: str, raw_args: dict) -> Tuple[Optional[dict], List[str]]:
    spec = get_manifest().get(kind)
    if not spec:
        return None, [f"Unknown job kind “{kind}”."]

    # client may send hidden selections outside the manifest (e.g. dxf_single's
    # card_mode-paired select) — keep them if present
    args, errors = normalize_args(spec, raw_args)

    info = get_info()
    argv, cwd, env, title, warnings, errs = build_command(kind, args, load_settings(), info)
    errors += errs
    if errors:
        return None, errors

    job_id = uuid.uuid4().hex[:10]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    job: dict = {
        "id": job_id,
        "ts": time.time(),
        "kind": kind,
        "title": title,
        "cmd": _fmt_argv(argv),
        "status": "running",
        "exit_code": None,
        "log_file": str(LOGS_DIR / f"{job_id}.log"),
        "log_lines": [],
        "subs": [],
        "warnings": warnings,
        "started": time.time(),
        "ended": None,
        "duration": None,
        "proc": None,
    }
    log_f = open(job["log_file"], "w", encoding="utf-8")
    header = [f"$ {job['cmd']}", f"(cwd: {cwd})"]
    log_f.write("\n".join(header) + "\n\n")
    log_f.flush()
    job["log_lines"] = header
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **_proc_kwargs())
        job["proc"] = proc
        with JOBS_LOCK:
            JOBS[job_id] = job
        threading.Thread(target=_pump, args=(job, proc, log_f), daemon=True).start()
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
        s = line.decode("utf-8", "replace").rstrip("\n")
        job["log_lines"].append(s)
        log_f.write(s + "\n")
        log_f.flush()
        for q in list(job["subs"]):
            try:
                q.put(("line", s))
            except Exception:
                pass
    rc = proc.wait()
    if job.get("kill_requested"):
        status = "killed"
    elif rc == 0:
        status = "ok"
    else:
        status = "fail"
    job["status"] = status
    job["exit_code"] = rc
    job["ended"] = time.time()
    job["duration"] = round(job["ended"] - job["started"], 2)
    log_f.close()
    for q in list(job["subs"]):
        try:
            q.put(("done", status, rc))
        except Exception:
            pass
    if job.get("kind", "").startswith("fetch:"):
        invalidate_manifest_cache()
    _persist_jobs()


def kill_job(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job["status"] != "running":
        return False
    job["kill_requested"] = True
    proc = job.get("proc")
    try:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
        else:
            proc.terminate()
    except Exception:
        pass
    return True


def sse_stream(job_id: str, after: int):
    """Yield (event, data) tuples for one SSE subscriber of a job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        yield "done", json.dumps({"status": "missing"})
        return
    q = queue.Queue()
    job["subs"].append(q)
    sent = after - 1
    try:
        for i, line in enumerate(job["log_lines"]):
            if i >= after:
                yield "line", json.dumps({"i": i, "s": line})
                sent = i
        # job already finished: late subscribers can't receive its done marker
        if job["status"] != "running":
            yield "done", json.dumps({"status": job["status"], "exit_code": job.get("exit_code")})
            return
        while True:
            try:
                msg = q.get(timeout=15)
            except Exception:
                yield "ping", "{}"
                if job["status"] != "running":
                    yield "done", json.dumps({"status": job["status"], "exit_code": job.get("exit_code")})
                    return
                continue
            if msg[0] == "line":
                sent += 1
                yield "line", json.dumps({"i": sent, "s": msg[1]})
            elif msg[0] == "done":
                for i, line in enumerate(job["log_lines"]):
                    if i > sent:
                        yield "line", json.dumps({"i": i, "s": line})
                        sent = i
                yield "done", json.dumps({"status": job["status"], "exit_code": job.get("exit_code")})
                return
    finally:
        try:
            job["subs"].remove(q)
        except Exception:
            pass


# ============================================================================
# File sandbox
# ============================================================================

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


def reveal_path(path: Path) -> Optional[str]:
    """Reveal a path in the platform file manager. Returns an error string or None."""
    if not path.exists():
        return "path does not exist"
    try:
        if os.name == "nt":
            if path.is_dir():
                subprocess.Popen(["explorer", str(path)], **_proc_kwargs())
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if not path.is_dir() else "", str(path)] if not path.is_dir()
                             else ["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])
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
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/ui/"):
                return self._static(path[4:])
            if path == "/favicon.svg":
                return self._static("favicon.svg")
            if path == "/api/info":
                return self._json(get_info())
            if path == "/api/manifest":
                return self._json(get_manifest())
            if path == "/api/jobs":
                with JOBS_LOCK:
                    running = [
                        {"id": j["id"], "ts": j["ts"], "kind": j["kind"], "title": j["title"],
                         "status": j["status"], "exit_code": j["exit_code"], "cmd": j["cmd"],
                         "warnings": j.get("warnings", [])}
                        for j in sorted(JOBS.values(), key=lambda x: x["ts"], reverse=True)[:50]
                    ]
                ids = {r["id"] for r in running}
                hist = [h for h in read_persisted_jobs() if h["id"] not in ids]
                return self._json({"jobs": running + hist[:200]})
            m = re.fullmatch(r"/api/jobs/([\w-]+)/log", path)
            if m:
                with JOBS_LOCK:
                    job = JOBS.get(m.group(1))
                if not job:
                    return self._json({"error": "job not found"}, 404)
                return self._json({"lines": job["log_lines"], "status": job["status"],
                                    "exit_code": job["exit_code"], "cmd": job["cmd"]})
            m = re.fullmatch(r"/api/jobs/([\w-]+)/stream", path)
            if m:
                after = int((q.get("after") or ["0"])[0])
                return self._sse(m.group(1), after)
            if path == "/api/file":
                return self._file(q)
            if path == "/api/preview":
                return self._preview(q)
            if path == "/api/settings":
                return self._json(load_settings())
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
            if path == "/api/settings":
                body = self._body()
                settings = load_settings()
                for k in ("scm_dir", "extras_dir", "python", "port", "theme", "auto_open_browser", "onboarded"):
                    if k in body:
                        settings[k] = body[k]
                if isinstance(body.get("defaults"), dict):
                    settings["defaults"].update(body["defaults"])
                save_settings(settings)
                invalidate_manifest_cache()
                return self._json({"ok": True, "settings": settings})
            if path == "/api/offset":
                body = self._body()
                settings = load_settings()
                scm, _ = effective_dirs(settings)
                if not scm:
                    return self._json({"ok": False, "errors": ["SCM repo not found — set it in Settings."]}, 400)
                d = scm / "data"
                d.mkdir(parents=True, exist_ok=True)
                payload = {
                    "x_offset": int(float(body.get("x", 0) or 0)),
                    "y_offset": int(float(body.get("y", 0) or 0)),
                    "angle_offset": float(float(body.get("angle", 0) or 0)),
                }
                with open(d / "offset_data.json", "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=4)
                return self._json({"ok": True, "offset": payload})
            if path == "/api/reveal":
                body = self._body()
                p = Path(body.get("path", ""))
                if not str(p):
                    return self._json({"ok": False, "errors": ["no path"]}, 400)
                roots = allowed_roots(load_settings())
                if not p.is_absolute():
                    cand = next((r / p for r in roots if (r / p).exists()), None)
                    p = cand or roots[0] / p if roots else p
                if not _inside(p, roots):
                    return self._json({"ok": False, "errors": ["path is outside the allowed repos"]}, 403)
                err = reveal_path(p)
                return self._json({"ok": err is None, "errors": [err] if err else []})
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
        self._send(200, data, MIME.get(p.suffix.lower(), "application/octet-stream"))

    def _file(self, q):
        settings = load_settings()
        rel = (q.get("path") or [""])[0]
        reveal = (q.get("reveal") or ["0"])[0] == "1"
        if not rel:
            return self._json({"error": "no path"}, 400)
        roots = allowed_roots(settings)
        p = Path(rel)
        if not p.is_absolute():
            cand = next((r / rel for r in roots if (r / rel).exists()), None)
            p = cand or (roots[0] / rel if roots else rel)
        if not _inside(p, roots):
            return self._json({"error": "path outside sandbox"}, 403)
        if reveal:
            err = reveal_path(p)
            return self._json({"ok": err is None, "errors": [err] if err else []})
        if p.is_dir():
            listing = [
                {"name": c.name, "dir": c.is_dir(), "size": 0 if c.is_dir() else c.stat().st_size, "path": str(c)}
                for c in sorted(p.iterdir())
            ]
            return self._json({"dir": str(p), "items": listing})
        if not p.is_file():
            return self._json({"error": "not found"}, 404)
        data = p.read_bytes()
        self._send(200, data, MIME.get(p.suffix.lower(), "application/octet-stream"),
                   [("Content-Disposition", f'inline; filename="{p.name}"')])

    def _preview(self, q):
        kind = (q.get("kind") or [""])[0]
        args_json = (q.get("args") or ["{}"])[0]
        try:
            args = json.loads(args_json)
        except Exception:
            return self._json({"error": "bad args"}, 400)
        manifest = get_manifest()
        if kind not in manifest:
            return self._json({"error": "unknown kind"}, 404)
        normalized, errors = normalize_args(manifest[kind], args)
        argv, cwd, env, title, warnings, errs = build_command(kind, normalized, load_settings(), get_info(), write_deck=False)
        return self._json({
            "cmd": _fmt_argv(argv) if not errors else None,
            "cwd": str(cwd) if cwd else None,
            "env": {k: v for k, v in env.items()
                     if k.startswith("SCM_") or k in ("PYTHONIOENCODING", "PYTHONUTF8")},
            "warnings": warnings + errors,
            "errors": errs,
        })

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

def main():
    ap = argparse.ArgumentParser(description="SCM Workbench — local UI for silhouette-card-maker + scm-extras")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    args = ap.parse_args()

    settings = load_settings()
    port = args.port or int(settings.get("port") or DEFAULT_PORT)
    scm, extras = effective_dirs(settings)

    out = io.StringIO()
    w = out.write
    w("\n")
    w("  \x1b[1;1mSCM Workbench\x1b[0m  v%s\n" % SERVER_VERSION)
    w("  ───────────────────────────────────────────────────────\n")
    w("  SCM repo:      %s\n" % (scm if scm else "\x1b[31mnot found — point Settings at it\x1b[0m"))
    w("  Extras repo:   %s\n" % (extras if extras else "\x1b[33mnot found (optional)\x1b[0m"))
    w("  Python:        %s\n" % sys.version.split()[0])
    w("  ───────────────────────────────────────────────────────\n")
    url = f"http://{args.host}:{port}"
    w(f"  UI:  {url}\n\n")
    w("  Local only — not exposed to your network. Ctrl+C to stop.\n")
    sys.stdout.write(out.getvalue())
    sys.stdout.flush()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True

    if not args.no_browser and settings.get("auto_open_browser", True):
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
