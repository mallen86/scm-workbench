/* pages/fetch — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, el, fmtBytes, ico, pageHead, toast } from "../core.js";
import { afterFormChange, defaultArgs, formCard } from "../forms.js";
import { go } from "../nav.js";
import { jobStrip } from "../jobstrip.js";

/* ================================ fetch page =============================== */

PAGES.fetch = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Fetch card art", "Pick a game, give it a decklist (from the list, a file you browse to on disk, or pasted text) and a format. The plugin downloads the card images into game/front/ (and game/double_sided/ where applicable) — ready for the PDF step."));
  const picker = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("download")),
      el("div", { class: "grow" }, el("h2", {}, "Game"), el("p", {}, "One plugin per game. Formats and options adapt to your pick.")),
    ),
  );
  const grid = el("div", { class: "plugin-grid" });
  const slugs = Object.keys(S.manifest).filter(k => k.startsWith("fetch:")).sort();
  for (const kind of slugs) {
    const slug = kind.slice(6);
    grid.append(el("button", {
      class: `plugin-card ${S.plugin === slug ? "active" : ""}`,
      onclick: () => { S.plugin = slug; go("fetch", { plugin: slug }); },
    },
      el("div", { class: "pc-t" }, S.manifest[kind].game),
      el("div", { class: "pc-f" }, `${(S.manifest[kind].groups.find(g => g.title === "Format")?.options[0].choices || []).length - 1} formats`),
    ));
  }
  picker.append(grid);
  wrap.append(picker);

  const kind = "fetch:" + S.plugin;
  if (!S.info.scm.found) {
    wrap.append(el("div", { class: "banner err" }, el("span", { class: "b-ico" }, ico("alert")), el("span", { class: "grow" }, "SCM repo not connected — the plugins live inside it. Fix it in Settings.")));
    return wrap;
  }
  wrap.append(formCard(kind, { icon: "download" }));
  wrap.__patch = () => patchFetchForm(kind);
  // simple mode: the console is hidden, so the page shows its own compact
  // status for the job — a progress bar while it runs, then the result with
  // the next step (create the PDF) one click away.
  wrap.append(jobStrip(kind, {
    icon: "download",
    runningLabel: `Fetching ${S.manifest[kind].game} card art`,
    onOk: (done, body) => {
      body.append(el("div", { class: "js-msg ok" },
        ico("check"), el("span", {}, "Card art is ready — the images are in place for the PDF.")));
      body.append(el("div", { class: "js-actions" },
        el("button", { class: "btn primary", onclick: () => go("pdf") }, ico("arrow"), "Go to Create PDF")));
    },
  }));
  return wrap;
};


/* fetch-specific control behavior: file list UI + paste visibility */
export function patchFetchForm(kind) {
  const args = S.forms[kind] || (S.forms[kind] = defaultArgs(kind));
  const card = $(`.form-card[data-kind="${kind}"]`);
  if (!card) return;
  const files = S.info.scm.decklists || [];
  const src = $$(".field", card).find(f => f.dataset.key === "deck_source");
  const fileF = $$(".field", card).find(f => f.dataset.key === "deck_file");
  const nameF = $$(".field", card).find(f => f.dataset.key === "deck_name");
  const textF = $$(".field", card).find(f => f.dataset.key === "deck_text");
  const urlF = $$(".field", card).find(f => f.dataset.key === "deck_url");
  if (fileF) {
    fileF.innerHTML = "";
    const label = el("label", { class: "fp-label" }, "Decklist file ", el("span", { class: "req" }, "*"));
    // In the app's own window we can open the native OS file chooser; in a
    // browser (dev mode) the button doesn't exist and the folder list is it.
    const canPick = !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_file);
    if (canPick) {
      const browse = el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
        title: "Pick any file on disk — it's copied into game/decklist/ and appears in the list",
        onclick: async () => {
          browse.disabled = true;
          let picked = null;
          try {
            picked = await window.pywebview.api.pick_file();
          } catch (e) {
            browse.disabled = false;
            toast("warn", "The file picker didn't open — paste the decklist text instead.");
            return;
          }
          browse.disabled = false;
          if (!picked) return; // cancelled in the panel
          let r;
          try {
            r = await api("/api/decklists/import", { path: picked });
          } catch (e) {
            toast("err", e.message);
            return;
          }
          if (r.ok) {
            S.info.scm.decklists = r.decklists;
            args.deck_file = r.name;
            fillList();
            autoFormat(); // .xml decklist → this game's XML-based format (e.g. MPCFill XML)
            afterFormChange(kind);
            toast("ok", `Imported “${r.name}” into the decklist folder`);
          } else {
            toast("err", (r.errors || [])[0] || "Importing the file failed.");
          }
        },
      }, ico("folder"), "Browse…");
      label.append(browse);
    }
    fileF.append(label);
    const list = el("div", { class: "filepick" });
    const fillList = () => {
      const fl = S.info.scm.decklists || [];
      list.innerHTML = "";
      if (!fl.length) list.append(el("div", { class: "small faint" },
        "No decklist files in game/decklist/ yet — use “Paste text” to create one" + (canPick ? ", or pick an existing file with Browse…" : "")));
      for (const f of fl) {
        list.append(el("div", {
          class: `fp-item ${args.deck_file === f.name ? "active" : ""}`,
          onclick: (ev) => {
            args.deck_file = f.name;
            $$(".fp-item", list).forEach(n => n.classList.remove("active"));
            ev.currentTarget.classList.add("active");
            autoFormat(); // .xml decklist → this game's XML-based format (e.g. MPCFill XML)
            afterFormChange(kind);
          },
        }, ico("file"), f.name, el("span", { class: "sz" }, fmtBytes(f.size))));
      }
    };
    fillList();
    fileF.append(list);
  }
  // if there's nothing to pick from, start in "paste" mode — before the UI syncs
  const seg = src ? $(".seg", src) : null;
  if (!files.length && args.deck_source === "file") {
    args.deck_source = "paste";
    if (seg) $$("button", seg).forEach(b => b.classList.toggle("active", b.textContent.trim() === "Paste text"));
  }
  const sync = () => {
    const mode = args.deck_source;
    if (nameF) nameF.style.display = mode === "paste" ? "" : "none";
    if (textF) textF.style.display = mode === "paste" ? "" : "none";
    if (urlF) urlF.style.display = mode === "url" ? "" : "none";
    if (fileF) fileF.style.display = mode === "file" ? "" : "none";
  };
  // Source-based format auto-selection (mirrors the rule the command builder
  // enforces server-side):
  //  - URL source: the format must be one of this game's URL-based formats,
  //    otherwise the URL would be passed to a file-reading parser (or rejected).
  //  - Existing-file source: picking an .xml decklist selects one of this
  //    game's XML-based formats (MTG: MPCFill XML, Final Fantasy: OctGN XML).
  const autoFormat = () => {
    const spec = S.manifest[kind] || {};
    const allowed = args.deck_source === "url" ? spec.url_formats || []
      : (args.deck_source === "file" && /\.xml$/i.test(args.deck_file || "") ? spec.xml_formats || [] : null);
    if (!allowed || !allowed.length || allowed.includes(args.format)) return;
    args.format = allowed[0];
    const fmtF = $$(".field", card).find(f => f.dataset.key === "format");
    const sel = fmtF && $("select", fmtF);
    if (sel) sel.value = args.format;
    afterFormChange(kind);
  };
  sync();
  autoFormat();
  if (seg) $$("button", seg).forEach(b => b.addEventListener("click", () => setTimeout(() => { sync(); autoFormat(); }, 0)));
}
