#!/usr/bin/env python3
"""Executable contracts for the Fetch game recently-used picker."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "ui" / "js"


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    fetch = (JS / "pages" / "fetch.js").read_text(encoding="utf-8")
    forms = (JS / "forms.js").read_text(encoding="utf-8")
    console = (JS / "console.js").read_text(encoding="utf-8")
    css = (ROOT / "ui" / "theme.css").read_text(encoding="utf-8")

    for marker in (
        'pluginSection("Recently used", layout.recent)',
        'pluginSection("All games", layout.all)',
        'el("details", { class: "plugin-all" }',
        "allGames.open = layout.allOpen",
        "document.addEventListener(JOBS_UPDATED_EVENT, onJobsUpdated)",
        "document.removeEventListener(JOBS_UPDATED_EVENT, onJobsUpdated)",
        'type: "button"',
        '"aria-pressed"',
    ):
        if marker not in fetch:
            return fail(f"Fetch game picker is missing {marker}")
    for source_name, source in (("forms.js", forms), ("console.js", console)):
        if 'import { publishJobsUpdated } from "./job-events.js";' not in source:
            return fail(f"{source_name} does not import the shared jobs-updated event")
        if "publishJobsUpdated();" not in source:
            return fail(f"{source_name} does not publish after updating shared jobs")
    for marker in (
        ".plugin-sections", ".plugin-section-title", ".plugin-all > summary",
        ".plugin-all[open] .plugin-summary-arrow", ".plugin-grid",
    ):
        if marker not in css:
            return fail(f"Fetch picker styling is missing {marker}")

    node = subprocess.run(
        ["node", "--input-type=module", "-", str(JS / "fetch-recents.js"),
         str(JS / "job-events.js")],
        input=r'''
import fs from "node:fs";
const dataUrl = source => "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const fail = message => { console.error(`FAIL: ${message}`); process.exit(1); };
const recents = await import(dataUrl(fs.readFileSync(process.argv[2], "utf8")));
const events = await import(dataUrl(fs.readFileSync(process.argv[3], "utf8")));
const available = ["altered", "mtg", "pokemon", "lorcana", "digimon", "keyforge", "netrunner"];

const empty = recents.recentFetchLayout([], available);
if (empty.hasRecent || !empty.allOpen || empty.recent.length || empty.all.length !== available.length)
  fail("a user without fetch history does not start with all games expanded");

const jobs = [
  {kind:"fetch:mtg", ts:10},
  {kind:"fetch:unknown", ts:90},
  {kind:"clean_up", ts:80},
  {kind:"fetch:mtg", ts:20},
  {kind:"fetch:pokemon", ts:30},
  {kind:null, ts:100},
];
const used = recents.recentFetchLayout(jobs, available);
if (used.recent.join() !== "pokemon,mtg" || !used.hasRecent || used.allOpen)
  fail("recent games are not distinct, newest-first, and collapsed over the full list");
const repeated = recents.recentFetchLayout(jobs, available);
if (repeated.allOpen) fail("the full list did not stay collapsed after a game was used");

const many = available.map((slug, index) => ({kind:`fetch:${slug}`, ts:index + 1}));
const bounded = recents.recentFetchPlugins(many, available);
if (bounded.length !== recents.RECENT_FETCH_LIMIT || bounded.join() !== "netrunner,keyforge,digimon,lorcana,pokemon,mtg")
  fail("the recently-used list is not bounded to six newest games");
if (recents.recentFetchPlugins("bad", null).length)
  fail("malformed job history was not ignored");

let dispatched = 0;
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
globalThis.document = { dispatchEvent(event) {
  if (event.type === events.JOBS_UPDATED_EVENT) dispatched += 1;
} };
events.publishJobsUpdated();
if (dispatched !== 1) fail("the shared jobs-updated event was not published");
console.log("ok: Fetch recents derive a bounded MRU list from canonical job history");
''',
        text=True,
        capture_output=True,
        timeout=20,
    )
    if node.returncode:
        print(node.stdout, end="")
        print(node.stderr, end="")
        return node.returncode
    print(node.stdout.strip())
    print("ok: initial all-games and used-game disclosure wiring is present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
