#!/usr/bin/env python3
"""Check that the shared confirmation dialog settles on every way out.

Callers hold busy flags (update install, custom-art imports, card-back
selection) across ``await confirmModal(...)``. A dialog dismissed without
settling its promise strands those flags until the window reloads.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "ui" / "js" / "core.js"


def main() -> int:
    node = r'''
import { pathToFileURL } from "node:url";

class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {};
    this.hidden = false; this.className = ""; this.textContent = "";
  }
  set innerHTML(value) { this.children = []; this._html = value; }
  get innerHTML() { return this._html || ""; }
  append(...kids) { this.children.push(...kids); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { for (const fn of this.listeners.click || []) fn({ currentTarget: this }); }
  querySelector(selector) {
    if (selector === ".modal") return modal;
    if (selector === ".modal-backdrop") return backdrop;
    return null;
  }
  get nodeType() { return 1; }
}
const root = new Node("div"); root.hidden = true;
const modal = new Node("div");
const backdrop = new Node("div");
const keyListeners = new Set();
globalThis.document = {
  createElement: tag => new Node(tag),
  createTextNode: text => ({ nodeType: 3, textContent: text }),
  querySelector: selector => (selector === "#modal-root" ? root : null),
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { if (type === "keydown") keyListeners.add(fn); },
  removeEventListener: (type, fn) => { if (type === "keydown") keyListeners.delete(fn); },
};
const press = key => { for (const fn of [...keyListeners]) fn({ key }); };
const buttons = () => modal.children.at(-1).children;
const settle = (promise, ms = 50) => Promise.race([
  promise, new Promise(resolve => setTimeout(() => resolve("unsettled"), ms)),
]);
const fail = message => { console.error(message); process.exit(1); };

const { confirmModal } = await import(pathToFileURL(process.argv[1]).href);

let pending = confirmModal({ title: "Escape" });
if (root.hidden) fail("dialog did not open");
press("Escape");
if (await settle(pending) !== false) fail("Escape must settle the dialog as cancelled");
if (!root.hidden || keyListeners.size) fail("Escape must close the dialog and remove its key listener");

pending = confirmModal({ title: "Confirm" });
press("Enter");
buttons()[1].click();
if (await settle(pending) !== true) fail("the confirm button must settle true");
if (keyListeners.size) fail("confirming must remove the key listener");

pending = confirmModal({ title: "Cancel" });
buttons()[0].click();
if (await settle(pending) !== false) fail("the cancel button must settle false");

pending = confirmModal({ title: "Backdrop" });
backdrop.onclick();
press("Escape");
if (await settle(pending) !== false) fail("the backdrop must settle false exactly once");
console.log("ok: confirmation dialog settles once on confirm, cancel, backdrop, and Escape");
'''
    result = subprocess.run(["node", "--input-type=module", "-e", node, str(CORE)],
                            text=True, capture_output=True)
    if result.returncode:
        print(f"FAIL: {(result.stderr or result.stdout).strip()}")
        return 1
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
