/* pages/templates — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, confirmModal, el, ico, pageHead, toast } from "../core.js";import { openFile } from "../native-actions.js";import { deleteTemplate } from "../template-transport.js";import { formCard } from "../forms.js";import { connectCardNeeded, repoSetupCard } from "../repo-setup.js";/* ============================== templates page ============================= */

PAGES.templates = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Cutting templates", "DXF templates for standard repo sizes, plus prebuilt .studio3 files for Silhouette Studio. See Extras for MTG and Sorcery sizes."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  wrap.append(formCard("dxf_single", { icon: "scissors" }));
  wrap.__patch = () => patchDxfForm("dxf_single");  // must run once the card is in the document
  wrap.append(formCard("dxf_batch", { icon: "layers" }));
  wrap.append(el("div", { class: "section-label" }, "Existing templates"));
  wrap.append(templatesGallery("scm"));
  return wrap;
};


export function patchDxfForm(kind) {
  const card = $(`.form-card[data-kind="${kind}"]`);
  if (!card) return;
  const args = S.forms[kind];
  const sync = () => {
    const custom = v => args[v] === "custom";
    for (const key of ["card_size", "card_width", "card_height", "card_radius", "card_name",
      "paper_size", "paper_width", "paper_height", "paper_name"]) {
      const f = $(`.field[data-key="${key}"]`, card);
      if (!f) continue;
      const show = key === "card_size" || key === "paper_size" ? !custom(key.replace("_size", "_mode"))
        : custom(key.replace("_width", "_mode").replace("_height", "_mode").replace("_radius", "_mode").replace("_name", "_mode"));
      if (show) { f.style.display = ""; if (f.querySelector(".frow-field")) {} } else f.style.display = "none";
    }
  };
  // re-bind: listen on the two mode segments
  for (const key of ["card_mode", "paper_mode"]) {
    const f = $(`.field[data-key="${key}"]`, card);
    if (f) $$("button", $(".seg", f)).forEach(b => b.onclick = () => setTimeout(sync, 0));
  }
  sync();
}


export function templatesGallery(which) {
  const info = which === "scm" ? S.info.scm : S.info.extras;
  const base = info.path ? info.path + "/cutting_templates" : null;
  const card = el("div", { class: "card" });
  const sections = [
    ["Default DXF", info.templates.dxf, "dxf", "/dxf/"],
    ["Borderless DXF", info.templates.borderless_dxf, "dxf", "/borderless/dxf/"],
    ["Default .studio3", info.templates.studio3, "studio3", "/"],
    ["Borderless .studio3", info.templates.borderless_studio3, "studio3", "/borderless/"],
  ];
  card.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("folder")),
    el("div", { class: "grow" }, el("h2", {}, which === "scm" ? "Repo cutting templates" : "Extras cutting templates"), el("p", {}, "DXF files are source files for Silhouette Studio. .studio3 files are preconfigured cutting jobs with paper size and registration marks.")),
  ));
  if (!base) card.append(el("div", { class: "empty" }, ico("folder"), "Repo not connected."));
  for (const [title, items, ext, dir] of sections) {
    if (!items?.length || !base) continue;
    const sectionLabel = el("div", { class: "section-label" }, title);
    card.append(sectionLabel);
    const g = el("div", { class: "filegrid" });
    for (const n of items) {
      const path = base + dir + n;
      const tile = el("div", { class: "fileitem-wrap" });
      tile.append(el("button", { class: "fileitem",
        title: `Open ${n} in the default app, such as Silhouette Studio`,
        onclick: async () => {
          try {
            const r = await openFile(path);
            if (r.ok) toast("ok", `Opening ${n} in the default app`);
            else toast("warn", r.errors?.[0] || r.error || `Couldn't open ${n}.`);
          } catch (error) {
            toast("warn", error?.message || `Couldn't open ${n}.`);
          }
        } },
        el("span", { class: "fi-ico" }, ico(ext === "dxf" ? "scissors" : "card")),
        el("span", { class: "fi-name" }, n),
      ));
      if (ext === "dxf") {
        tile.append(el("button", {
          class: "fileitem-delete", type: "button",
          title: `Delete ${n}`, "aria-label": `Delete ${n}`,
          onclick: async () => {
            const confirmed = await confirmModal({
              title: "Delete cutting template?",
              text: `Permanently delete “${n}”? This cannot be undone.`,
              okLabel: "Delete", danger: true, icon: "trash", iconCls: "warn",
            });
            if (!confirmed) return;
            try {
              await deleteTemplate(path);
              const index = items.indexOf(n);
              if (index >= 0) items.splice(index, 1);
              tile.remove();
              if (!items.length) { sectionLabel.remove(); g.remove(); }
              toast("ok", `Deleted ${n}.`);
            } catch (error) {
              toast("warn", error?.message || `Couldn't delete ${n}.`);
            }
          },
        }, "×"));
      }
      g.append(tile);
    }
    card.append(g);
  }
  return card;
}
