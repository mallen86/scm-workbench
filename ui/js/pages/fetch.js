/* pages/fetch — part of the SCM Workbench UI (vanilla ES modules, no build
   step; the entry point is ui/js/app.js, which imports every page). */

import { $, $$, PAGES, S, api, confirmModal, el, fmtBytes, ico, pageHead, toast } from "../core.js";
import { canImportDecklist, importDecklist } from "../decklist-transport.js";import { afterFormChange, defaultArgs, doRun, formCard } from "../forms.js";import { go, uiMode } from "../nav.js";import { clearJobCompletion, jobStrip } from "../jobstrip.js";import { watchJobDone } from "./utilities.js";/* ================================ fetch page =============================== */

PAGES.fetch = (root) => {
  const wrap = el("div", {});
  wrap.append(pageHead("Fetch card art", "Choose a game, decklist, and format. Decklists can come from the list, a file, or pasted text. The plugin saves images to game/front/ and, when applicable, game/double_sided/ for the PDF step."));
  const picker = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("download")),
      el("div", { class: "grow" }, el("h2", {}, "Game"), el("p", {}, "Each game has one plugin. Formats and options match your selection.")),
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
    wrap.append(el("div", { class: "banner err" }, el("span", { class: "b-ico" }, ico("alert")), el("span", { class: "grow" }, "SCM repo is not connected. Plugins are stored there. Fix this in Settings.")));
    return wrap;
  }
  wrap.append(formCard(kind, { icon: "download", flat: uiMode() === "simple", head: uiMode() !== "simple" }));
  wrap.__patch = () => patchFetchForm(kind);

  // The plugins never delete existing images: fetching a *different* deck
  // leaves the old art in game/front/ and the next PDF mixes it in. Keep a
  // one-click clear on this page (Simple mode has no Utilities page).
  const cc = el("div", { class: "card" });
  cc.append(el("div", { class: "card-head" },
    el("div", { class: "card-ico" }, ico("trash")),
    el("div", { class: "grow" }, el("h2", {}, "Starting a different deck?"),
      el("p", {}, "Fetching does not delete existing images. Clear the folders first to keep old art out of your new PDF."))));
  cc.append(el("div", { class: "runbar" },
    el("span", { class: "rb-note" }, "Deletes images in game/front/ and game/double_sided/. Card backs stay. This cannot be undone."),
    el("button", { class: "btn danger", onclick: async () => {
      const ok = await confirmModal({ title: "Delete card images?", text: "Images in game/front/ and game/double_sided/ will be permanently deleted. Card backs stay.", okLabel: "Yes, clear them", danger: true, icon: "trash", iconCls: "warn" });
      if (!ok) return;
      const job = await doRun("clean_up", null);
      if (job) {
        // Cleanup has begun, so the prior fetch can no longer promise that its
        // images are ready. Hide it now and suppress both ways a rebuilt strip
        // could resurrect it; the cleanup job itself remains in job history.
        clearJobCompletion(kind);
        watchJobDone(job.id, () => afterFormChange(kind));   // the preview re-queries, so the stale-images warning clears
      }
    } }, ico("trash"), "Clear card images")));
  wrap.append(cc);
  // simple mode: the console is hidden, so the page shows its own compact
  // status for the job — a progress bar while it runs, then the result with
  // the next step (create the PDF) one click away.
  wrap.append(jobStrip(kind, {
    icon: "download",
    runningLabel: `Fetching ${S.manifest[kind].game} card art`,
    // A prefetching fetch runs a second stage over the decklist's slots. The
    // server records how many the decklist declares; without it the bar falls
    // back to the number of images prefetched.
    slotTotal: async job => job.deck_total || 0,
    onOk: (done, body) => {
      body.append(el("div", { class: "js-msg ok" },
        ico("check"), el("span", {}, "Card art is ready. Images are in place for the PDF.")));
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
    const canImport = canImportDecklist();
    if (canImport) {
      const browse = el("button", {
        class: "btn btn-ghost btn-sm", type: "button",
        title: "Choose a file on disk. It is copied to game/decklist/ and added to the list.",
        onclick: async () => {
          browse.disabled = true;
          try {
            const result = await importDecklist();
            if (result === null) {
              browse.disabled = false;
              return;
            }
            if (!result?.ok) {
              browse.disabled = false;
              toast("err", (result?.errors || [])[0] || "Importing the file failed.");
              return;
            }
            S.info.scm.decklists = result.decklists;
            args.deck_file = result.name;
            fillList();
            autoFormat(); // .xml decklist → this game's XML-based format (e.g. MPCFill XML)
            afterFormChange(kind, args);
            browse.disabled = false;
            toast("ok", `Imported “${result.name}” into the decklist folder.`);
            return;
          } catch (e) {
            browse.disabled = false;
            toast("warn", `The file picker could not open (${e.message || "unknown error"}). Paste the decklist text instead.`);
            return;
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
        "No decklist files are in game/decklist/ yet. Use “Paste text” to create one" + (canImport ? ", or browse for an existing file" : "")));
      for (const f of fl) {
        list.append(el("div", {
          class: `fp-item ${args.deck_file === f.name ? "active" : ""}`,
          onclick: (ev) => {
            args.deck_file = f.name;
            $$(".fp-item", list).forEach(n => n.classList.remove("active"));
            ev.currentTarget.classList.add("active");
            autoFormat(); // .xml decklist → this game's XML-based format (e.g. MPCFill XML)
            afterFormChange(kind, args);
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
    afterFormChange(kind, args);
  };
  sync();
  autoFormat();
  if (seg) $$("button", seg).forEach(b => b.addEventListener("click", () => setTimeout(() => { sync(); autoFormat(); }, 0)));
}
