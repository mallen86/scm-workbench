#!/usr/bin/env python3
"""Static and Node contract for the card-back import boundary."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"


def fail(message):
    print(f"FAIL: {message}")
    return 1


def main():
    transport = (UI / "back-image-transport.js").read_text(encoding="utf-8")
    page = (UI / "pages" / "pdf.js").read_text(encoding="utf-8")
    state_source = (UI / "back-image-state.js").read_text(encoding="utf-8")
    if 'invoke("wb_back_image_import", {})' not in transport:
        return fail("card-back import is not a no-argument native command")
    if 'fetch("/api/back-images/import"' not in transport:
        return fail("browser compatibility route is missing")
    if "native picker required" not in transport:
        return fail("browser import does not require an explicit path")
    if "backImageCard" in page or "installBackImageControl" not in page:
        return fail("card-back selection is still a separate workflow card")
    if ('$(".runbar", card)?.before(control)' not in page or
            '$(".field[data-key=back_dir]", card)' not in page):
        return fail("card-back selection is not placed before Run and attached to the Advanced folder field")
    if "currentOnlyFronts" not in page or "listFiles(directory, true)" not in page:
        return fail("card-back state does not follow the active PDF options")
    if "Choose image" not in page or "Reveal folder" not in page:
        return fail("Create PDF does not expose the card-back actions")
    if 'afterFormChange("create_pdf", S.forms.create_pdf);' not in page:
        return fail("a card-back import does not refresh the Create PDF preview")
    if ('title: "Replace the card back image?"' not in page or
            'okLabel: "Choose replacement"' not in page or 'if (!replace) return;' not in page):
        return fail("replacing existing card-back images is not confirmed")
    node = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const transportSource = fs.readFileSync(process.argv[3], "utf8");
const stateSource = fs.readFileSync(process.argv[4], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const facade = await import(dataUrl(source.replace('from "./transport.js"', `from "${dataUrl(transportSource)}"`)));
const state = await import(dataUrl(stateSource));
if (state.backDirectory("  ") !== "game/back" || !state.isDefaultBackDirectory("./game/back/")) throw new Error("default directory normalization failed");
if (state.isDefaultBackDirectory("custom/back")) throw new Error("custom directory treated as default");
const one = state.backImageState({ items: [{ name: "back.png" }], found: 1 });
if (one.status !== "back.png" || !one.canImport || !one.canReveal || one.tone) throw new Error("single default back state failed");
const multiple = state.backImageState({ items: [{ name: "a.png" }, { name: "b.jpg" }], found: 2 });
if (multiple.status !== "2 recognized images. Keep exactly one." || multiple.tone !== "warn") throw new Error("multiple back warning failed");
const custom = state.backImageState({ dir: "custom/back", items: [{ name: "custom.png" }], found: 1 });
if (custom.defaultDirectory || custom.canImport || !custom.canReveal || custom.status !== "custom.png") throw new Error("custom directory state failed");
const fronts = state.backImageState({ onlyFronts: true, items: [{ name: "unused.png" }], found: 1 });
if (fronts.status !== "Not used for front pages only." || fronts.canImport || fronts.canReveal || fronts.tone !== "muted") throw new Error("front-only state failed");
const missing = state.backImageState({ dir: "missing", exists: false });
if (missing.status !== "Folder not found." || missing.canReveal || missing.tone !== "warn") throw new Error("missing folder state failed");
const truncated = state.backImageState({ dir: "large", truncated: true });
if (truncated.status !== "Could not safely check every file." || truncated.tone !== "warn") throw new Error("truncated folder state failed");
let calls = [], fetchCalls = [], mode = "ok";
const internals = { invoke(method, params) {
  calls.push({ method, params, receiver: this });
  return mode === "reject" ? Promise.reject(new Error("native failure")) : Promise.resolve(mode === "cancel" ? null : { ok: true, name: "back.png", back_images: [{ name: "back.png" }] });
} };
globalThis.window = { __TAURI_INTERNALS__: internals };
globalThis.fetch = (...args) => { fetchCalls.push(args); return Promise.reject(new Error("HTTP fallback")); };
if (!facade.canImportBackImage() || JSON.stringify(await facade.importBackImage("ignored")) !== JSON.stringify({ok:true,name:"back.png",back_images:[{name:"back.png"}]}) || calls[0].method !== "wb_back_image_import" || JSON.stringify(calls[0].params) !== "{}" || fetchCalls.length) throw new Error("native payload failed");
mode = "cancel"; if (await facade.importBackImage() !== null) throw new Error("native cancellation failed");
mode = "reject"; let rejected = false; try { await facade.importBackImage(); } catch (error) { rejected = error.message === "native failure"; }
if (!rejected || fetchCalls.length) throw new Error("native rejection fell back");
delete globalThis.window; let browserError = false; try { await facade.importBackImage(); } catch (error) { browserError = error.message === "native picker required"; }
if (!browserError) throw new Error("browser exposed picker");
fetchCalls = [];
globalThis.fetch = (...args) => {
  fetchCalls.push(args);
  return Promise.resolve({ ok: true, json: async () => ({ ok: true, name: "browser.png", back_images: [{ name: "browser.png" }] }) });
};
const browserResult = await facade.importBackImage("/tmp/browser.png");
const browserOptions = fetchCalls[0]?.[1] || {};
if (browserResult.name !== "browser.png" || fetchCalls[0]?.[0] !== "/api/back-images/import" ||
    browserOptions.method !== "POST" || browserOptions.headers?.["Content-Type"] !== "application/json" ||
    browserOptions.body !== JSON.stringify({ path: "/tmp/browser.png" })) throw new Error("explicit browser path payload failed");
console.log("ok: card-back native payload, cancellation, rejection isolation, and explicit browser path pass");
'''.strip()
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(UI / "back-image-transport.js"),
         str(UI / "transport.js"), str(UI / "back-image-state.js")],
        input=node, text=True, capture_output=True,
    )
    if result.returncode:
        return fail((result.stderr or result.stdout).strip())
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
