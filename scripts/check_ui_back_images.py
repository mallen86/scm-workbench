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
    if 'invoke("wb_back_image_import", {})' not in transport:
        return fail("card-back import is not a no-argument native command")
    if 'fetch("/api/back-images/import"' not in transport:
        return fail("browser compatibility route is missing")
    if "native picker required" not in transport:
        return fail("browser import does not require an explicit path")
    if "backImageCard" not in page or "Choose image" not in page or "Reveal folder" not in page:
        return fail("Create PDF does not expose the card-back state and actions")
    if 'afterFormChange("create_pdf", S.forms.create_pdf);' not in page:
        return fail("a card-back import does not refresh the Create PDF preview")
    if ('title: "Replace the card back image?"' not in page or
            'okLabel: "Choose replacement"' not in page or 'if (!replace) return;' not in page):
        return fail("replacing existing card-back images is not confirmed")
    node = r'''
import fs from "node:fs";
const source = fs.readFileSync(process.argv[2], "utf8");
const transportSource = fs.readFileSync(process.argv[3], "utf8");
const dataUrl = value => `data:text/javascript;base64,${Buffer.from(value, "utf8").toString("base64")}`;
const facade = await import(dataUrl(source.replace('from "./transport.js"', `from "${dataUrl(transportSource)}"`)));
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
        ["node", "--input-type=module", "-", str(UI / "back-image-transport.js"), str(UI / "transport.js")],
        input=node, text=True, capture_output=True,
    )
    if result.returncode:
        return fail((result.stderr or result.stdout).strip())
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
