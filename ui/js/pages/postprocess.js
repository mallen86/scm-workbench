/* Advanced-only image processor library and run workflow. */
import { PAGES, S, $, $$, confirmModal, el, ico, pageHead, toast } from "../core.js";
import { doRun, formArgs, formCard } from "../forms.js";
import { jobs } from "../jobs.js";
import { go, uiMode } from "../nav.js";
import { postprocessors } from "../postprocess-transport.js";

const TEMPLATE = `from pathlib import Path\n\n\ndef process_image(image_path: Path, context: dict) -> None:\n    """Modify the private working copy in place."""\n    # Open image_path, transform it, and save it back to image_path.\n    return None\n`;

const state = { processors: [], selected: null, draft: null, loaded: null, dirty: false, job: null, sub: null, timer: null };
const first = value => Array.isArray(value) ? value[0] : value;
const revision = p => p?.revision_hash || p?.revision || p?.active_revision || "";
const normalizeList = result => Array.isArray(result) ? result : (result?.processors || []);
const isReady = p => p && (p.environment_ready === true || p.dependencies === "ready" || p.environment?.status === "ready");
const isTrusted = p => !!p && (p.trusted === true || p.trust?.revision_hash === revision(p));
const selectedProcessor = () => state.processors.find(p => p.id === state.selected) || null;

function sourceBytes(value) { return new TextEncoder().encode(String(value || "")).length; }
function markDirty() { state.dirty = true; updateEditorState(); }
function updateEditorState() {
  const status = document.querySelector(".pp-dirty");
  if (status) { status.textContent = state.dirty ? "Unsaved changes" : "Saved revision"; status.className = `pp-dirty ${state.dirty ? "warn" : "ok"}`; }
  const save = document.querySelector(".pp-save");
  if (save) save.disabled = !state.draft?.name?.trim() || !state.draft?.source;
}
function setEditorValue(value) {
  const d = state.draft || (state.draft = { name: "New processor", source: TEMPLATE, requirements: "" });
  d.name = value?.name ?? d.name;
  d.source = value?.source ?? d.source;
  d.requirements = value?.requirements ?? d.requirements;
  const name = document.querySelector(".pp-name");
  const source = document.querySelector(".pp-source");
  const req = document.querySelector(".pp-requirements");
  if (name) name.value = d.name;
  if (source) source.value = d.source;
  if (req) req.value = d.requirements;
  state.dirty = false;
  updateEditorState();
}

async function refreshProcessors(selectId = state.selected) {
  let result;
  try { result = await postprocessors.list(); }
  catch (error) {
    state.processors = [];
    repaintLibrary();
    const box = document.querySelector(".pp-library-list");
    if (box) box.replaceChildren(el("div", { class: "empty" }, error.message || "Post-processing is not available yet."));
    return;
  }
  state.processors = normalizeList(result);
  const requested = selectId || S.postprocessPrefill?.processor_id;
  state.selected = state.processors.some(p => p.id === requested) ? requested : state.processors[0]?.id || null;
  if (state.selected) await loadProcessor(state.selected);
  else setEditorValue({ name: "New processor", source: TEMPLATE, requirements: "" });
  repaintLibrary();
  patchRunForm();
}

async function loadProcessor(id) {
  state.selected = id;
  const summary = selectedProcessor();
  if (!summary) return;
  try {
    const detail = await postprocessors.get(id);
    state.loaded = { ...summary, ...detail };
    setEditorValue({ name: state.loaded.name || "Processor", source: state.loaded.source || "", requirements: state.loaded.requirements || "" });
  } catch (error) { toast("err", error.message || "Could not load processor."); }
}

function repaintLibrary() {
  const box = document.querySelector(".pp-library-list");
  if (!box) return;
  box.replaceChildren();
  if (!state.processors.length) { box.append(el("div", { class: "empty" }, "No processors saved yet. Create one below.")); return; }
  for (const p of state.processors) {
    const active = p.id === state.selected;
    const trust = isTrusted(p), ready = isReady(p);
    box.append(el("div", { class: `pp-processor ${active ? "active" : ""}`, onclick: async () => {
      if (state.dirty && !await confirmModal({ title: "Discard unsaved changes?", text: "Your processor edits will be lost.", okLabel: "Discard changes", danger: true })) return;
      await loadProcessor(p.id); repaintLibrary(); patchRunForm();
    } },
      el("div", { class: "pp-processor-main" }, el("strong", {}, p.name || "Unnamed processor"), el("span", { class: "small faint mono" }, revision(p).slice(0, 12) || "no revision")),
      el("div", { class: "pp-processor-meta" }, el("span", { class: `chip ${trust ? "ok" : "warn"}` }, trust ? "Trusted" : "Untrusted"), el("span", { class: `chip ${ready ? "ok" : "warn"}` }, ready ? "Libraries ready" : (p.environment_status || "Libraries not ready"))),
      el("div", { class: "pp-actions" },
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: e => { e.stopPropagation(); duplicateProcessor(p); } }, "Duplicate"),
        el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: e => { e.stopPropagation(); deleteProcessor(p); } }, "Delete"),
      ),
    ));
  }
}

async function duplicateProcessor(p) {
  try { await postprocessors.duplicate(p.id, `${p.name || "Processor"} copy`, revision(p)); await refreshProcessors(); toast("ok", "Processor duplicated."); }
  catch (error) { toast("err", error.message || "Could not duplicate processor."); }
}
async function deleteProcessor(p) {
  if (!await confirmModal({ title: "Delete processor?", text: `Delete “${p.name || "processor"}” and its saved revisions?`, okLabel: "Delete processor", danger: true })) return;
  try { await postprocessors.delete(p.id, revision(p)); state.selected = null; await refreshProcessors(); toast("ok", "Processor deleted."); }
  catch (error) { toast("err", error.message || "Could not delete processor."); }
}

async function saveRevision() {
  const d = state.draft;
  if (!d?.name?.trim() || !d.source) return toast("warn", "Enter a processor name and source first.");
  if (sourceBytes(d.source) > 256 * 1024) return toast("err", "Processor source is too large.");
  try {
    const result = await postprocessors.save({ processor_id: state.selected, name: d.name.trim(), source: d.source, requirements: d.requirements, expected_revision: revision(state.loaded) || null });
    await refreshProcessors(result?.processor?.id || result?.id || state.selected);
    toast("ok", "Saved an untrusted revision. Trust it before running.");
  } catch (error) { toast("err", error.message || "Could not save the processor."); }
}

async function importSource() {
  try {
    const result = await postprocessors.importSource({ saveDraft: payload => postprocessors.save(payload) });
    if (result === null) return;
    if (result?.ok === false) throw new Error((result.errors || ["Import failed."])[0]);
    const id = result?.processor?.id || result?.id;
    if (id) await refreshProcessors(id); else setEditorValue(result);
    state.dirty = false;
    toast("ok", "Imported an untrusted revision. Trust it before running.");
  } catch (error) { toast("err", error.message || "Could not import the processor."); }
}
async function revert() {
  if (!state.dirty || await confirmModal({ title: "Revert unsaved changes?", text: "Your current editor contents will be replaced.", okLabel: "Revert changes", danger: true })) setEditorValue(state.loaded || { name: "New processor", source: TEMPLATE, requirements: "" });
}
async function trustRevision() {
  const p = state.loaded || selectedProcessor();
  if (!p) return toast("warn", "Save a processor before trusting it.");
  try { await postprocessors.trust(p.id, revision(p), p.environment_fingerprint || p.environment?.fingerprint || null); await refreshProcessors(p.id); toast("ok", "This exact revision is trusted."); }
  catch (error) { toast("err", error.message || "Could not trust this revision."); }
}
async function installLibraries() {
  const p = state.loaded || selectedProcessor();
  if (!p) return toast("warn", "Save a processor first.");
  try { await doRun("postprocess_dependencies", null, { args: { processor_id: p.id, revision_hash: revision(p), requirements: state.draft?.requirements || "" } }); toast("info", "Library installation started."); }
  catch (error) { toast("err", error.message || "Could not start library installation."); }
}

function patchRunForm() {
  const kind = "postprocess_images";
  const card = document.querySelector(`.form-card[data-kind="${kind}"]`);
  if (!card) return;
  const args = formArgs(kind);
  if (S.postprocessPrefill?.scope && args.scope !== undefined) args.scope = S.postprocessPrefill.scope;
  const procField = $$(".field", card).find(f => f.dataset.key === "processor_id");
  const select = procField && $("select", procField);
  if (select) {
    select.replaceChildren(...state.processors.map(p => el("option", { value: p.id, selected: p.id === state.selected }, p.name || p.id)));
    args.processor_id = state.selected || "";
  }
  const run = document.querySelector(".pp-run");
  const p = selectedProcessor();
  const gate = !p ? "Choose a processor" : !isTrusted(p) ? "Trust this exact revision first" : !isReady(p) ? "Install or rebuild libraries first" : "Ready to run";
  if (run) { run.disabled = gate !== "Ready to run"; run.title = gate; }
  const note = document.querySelector(".pp-run-note");
  if (note) note.textContent = gate;
}

function attachRunStatus(root) {
  const status = el("div", { class: "pp-run-status", "aria-live": "polite" });
  root.append(status);
  const paint = () => {
    const running = state.job && (S.jobs || []).find(j => j.id === state.job.id);
    if (!running) return;
    status.replaceChildren(el("div", { class: `pp-status ${running.status}` }, running.status === "running" ? "Processing images…" : running.status === "ok" ? "Processing complete. Original images were replaced after validation." : "Processing failed or was cancelled. Original images were not changed."));
    if (running.status === "running") {
      const done = Number(running.progress?.current || 0), total = Number(running.progress?.total || running.image_total || 0);
      status.append(el("progress", { max: total || 1, value: Math.min(done, total || 1) }), el("span", { class: "small faint" }, total ? `${done} / ${total}` : "Preparing image set…"));
    } else if (running.status === "ok") status.append(el("button", { class: "btn primary", onclick: () => go("pdf") }, ico("arrow"), "Go to Create PDF"));
  };
  let lastDependencyStatus = "";
  const tick = async () => { try {
    const result = await jobs.list();
    S.jobs = result.jobs || S.jobs;
    const dependency = (S.jobs || []).find(j => j.kind === "postprocess_dependencies" && j.status === "running");
    if (!dependency && lastDependencyStatus === "running") await refreshProcessors(state.selected);
    lastDependencyStatus = dependency ? "running" : "idle";
    paint();
  } catch {} };
  state.timer = setInterval(tick, 700); tick();
  root.__dispose = () => { clearInterval(state.timer); if (state.sub) state.sub.close(); state.sub = null; state.timer = null; if (S.pageGuard === root.__guard) S.pageGuard = null; };
}

PAGES.postprocess = root => {
  if (uiMode() === "simple") { go("fetch", null, { push: false }); return el("div", {}); }
  const wrap = el("div", {});
  wrap.append(pageHead("Image post-processing", "Save a trusted Python processor, install its optional libraries, then apply it manually to fetched card images."));
  wrap.append(el("div", { class: "banner warn pp-warning" }, ico("alert"), el("span", {}, "Python processors and their libraries run as your user account. Only use code and packages you trust. Workbench limits inputs, resources, and image publication, but it cannot safely sandbox arbitrary Python from your other files or network.")));
  const library = el("section", { class: "card pp-library" }, el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("layers")), el("div", { class: "grow" }, el("h2", {}, "Processor library"), el("p", {}, "Select a revision to edit, trust, install, or run."))), el("div", { class: "pp-library-list" }));
  wrap.append(library);
  const editor = el("section", { class: "card pp-editor" }, el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("file")), el("div", { class: "grow" }, el("h2", {}, "Processor editor"), el("p", {}, "Saving creates an immutable, untrusted revision.")), el("span", { class: "pp-dirty" }, "Saved revision")),
    el("label", {}, "Processor name", el("input", { class: "input pp-name", spellcheck: "false" })),
    el("label", {}, "Python source", el("textarea", { class: "input pp-source", rows: 16, spellcheck: "false" })),
    el("label", {}, "Optional requirements", el("textarea", { class: "input pp-requirements", rows: 4, spellcheck: "false", placeholder: "Pillow==10.4.0" })),
    el("div", { class: "runbar pp-editor-actions" }, el("span", { class: "rb-note" }, "Source is parsed when saved, never executed."), el("button", { class: "btn btn-ghost", type: "button", onclick: importSource }, "Import .py"), el("button", { class: "btn btn-ghost", type: "button", onclick: revert }, "Revert"), el("button", { class: "btn btn-ghost", type: "button", onclick: trustRevision }, "Trust this revision"), el("button", { class: "btn btn-ghost", type: "button", onclick: installLibraries }, "Install / rebuild libraries"), el("button", { class: "btn primary pp-save", type: "button", onclick: saveRevision }, "Save revision")));
  wrap.append(editor);
  const run = el("section", { class: "card pp-run-card" }, el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("play")), el("div", { class: "grow" }, el("h2", {}, "Run processor"), el("p", {}, "One isolated batch processes the selected image scope."))), formCard("postprocess_images", { run: false }), el("div", { class: "runbar" }, el("span", { class: "rb-note pp-run-note" }, "Choose a processor"), el("button", { class: "btn primary pp-run", type: "button", onclick: async e => { state.job = await doRun("postprocess_images", e.currentTarget); } }, ico("play"), "Run processor")));
  wrap.append(run);
  wrap.__patch = async () => {
    const source = $(".pp-source", wrap), req = $(".pp-requirements", wrap), name = $(".pp-name", wrap);
    source.oninput = () => { state.draft.source = source.value; markDirty(); };
    req.oninput = () => { state.draft.requirements = req.value; markDirty(); };
    name.oninput = () => { state.draft.name = name.value; markDirty(); };
    $(".pp-source", wrap).addEventListener("keydown", e => { if (e.key === "Tab") { e.preventDefault(); const at = e.target.selectionStart; e.target.setRangeText("    ", at, e.target.selectionEnd, "end"); state.draft.source = e.target.value; markDirty(); } });
    await refreshProcessors(); repaintLibrary(); patchRunForm(); attachRunStatus(wrap);
  };
  wrap.__guard = async () => !state.dirty || await confirmModal({ title: "Unsaved processor changes", text: "Leave this page and discard your edits?", okLabel: "Leave page", danger: true });
  S.pageGuard = wrap.__guard;
  return wrap;
};
