/* Simple run workflow plus the Advanced processor library and editor. */
import { PAGES, S, $, confirmModal, el, ico, pageHead, toast } from "../core.js";
import { afterFormChange, COMMAND_PREVIEW_EVENT, doRun, formArgs, formCard } from "../forms.js";
import { jobs } from "../jobs.js";
import { go, uiMode } from "../nav.js";
import { renderPythonHighlight } from "../python-highlight.js";
import { postprocessors } from "../postprocess-transport.js";
import { watchJobDone } from "./utilities.js";

const TEMPLATE = `from pathlib import Path\n\n\ndef process_image(image_path: Path, context: dict) -> None:\n    """Modify the private working copy in place."""\n    # Open image_path, transform it, and save it back to image_path.\n    return None\n`;

const state = { processors: [], selected: null, draft: null, loaded: null, loadError: false, dirty: false, installing: false, job: null, sub: null, timer: null, imageCount: null, imageScope: null };
const first = value => Array.isArray(value) ? value[0] : value;
const revision = p => p?.revision_hash || p?.revision || p?.active_revision || "";
const normalizeList = result => Array.isArray(result) ? result : (result?.processors || []);
const isReady = p => p && (p.environment_ready === true || p.dependencies === "ready" || p.environment?.status === "ready");
const isStale = p => p?.environment_status === "stale" || p?.environment?.status === "stale";
const isTrusted = p => !!p && (p.trusted === true || p.trust?.revision_hash === revision(p));
const canRun = p => !!p && (p.ready_to_run === true || (isReady(p) && isTrusted(p)));
const selectedProcessor = () => state.processors.find(p => p.id === state.selected) || null;

function sourceBytes(value) { return new TextEncoder().encode(String(value || "")).length; }
function updateLockSummary() {
  const box = document.querySelector(".pp-lock");
  if (!box) return;
  box.replaceChildren();
  if (state.dirty) {
    box.append(el("span", { class: "small faint" }, "Save this revision, then install libraries to resolve its exact dependency lock."));
    return;
  }
  if (isStale(state.loaded)) {
    box.append(el("span", { class: "small warn" }, "The selected Python runtime changed. Reinstall libraries for this processor, then review and trust it again."));
    return;
  }
  const wheels = state.loaded?.environment?.wheels || [];
  if (!wheels.length) {
    box.append(el("span", { class: "small faint" }, state.loaded?.requirements?.length ? "No resolved dependency lock is installed." : "No third-party wheels are required."));
    return;
  }
  box.append(el("strong", {}, "Resolved library lock"));
  for (const wheel of wheels) box.append(el("div", { class: "small mono" }, `${wheel.name}==${wheel.version}  sha256:${wheel.sha256.slice(0, 16)}…`));
}
function updateCursor(source = document.querySelector(".pp-source")) {
  const status = document.querySelector(".pp-cursor");
  if (!source || !status) return;
  const before = source.value.slice(0, source.selectionStart ?? 0).split("\n");
  status.textContent = `Line ${before.length}, column ${before.at(-1).length + 1}`;
}
function syncSourceHighlightScroll(source = document.querySelector(".pp-source")) {
  const highlight = source?.closest(".pp-source-wrap")?.querySelector(".pp-source-highlight");
  if (!source || !highlight) return;
  highlight.scrollTop = source.scrollTop;
  highlight.scrollLeft = source.scrollLeft;
}
function paintSourceHighlight(source = document.querySelector(".pp-source")) {
  const wrap = source?.closest(".pp-source-wrap");
  const code = wrap?.querySelector(".pp-source-code");
  if (!source || !wrap || !code) return;
  renderPythonHighlight(code, source.value);
  wrap.classList.add("highlight-ready");
  syncSourceHighlightScroll(source);
}
function markDirty() { state.dirty = true; updateEditorState(); updateLockSummary(); }
function updateEditorState() {
  const status = document.querySelector(".pp-dirty");
  if (status) { status.textContent = state.dirty ? "Unsaved changes" : "Saved revision"; status.className = `pp-dirty ${state.dirty ? "warn" : "ok"}`; }
  const bundled = state.loaded?.bundled === true;
  const save = document.querySelector(".pp-save");
  if (save) { save.disabled = bundled || state.loadError || !state.draft?.name?.trim() || !state.draft?.source; save.title = bundled ? "Built-in processors are read-only; duplicate it to customize" : "Save revision"; }
  const trust = document.querySelector(".pp-trust");
  if (trust) {
    trust.disabled = bundled || state.loadError || !state.loaded || state.dirty || !isReady(state.loaded);
    trust.title = bundled ? "Built-in processors are app-trusted" : !state.loaded ? "Save the processor first" : state.dirty ? "Save this revision first" : isStale(state.loaded) ? "Reinstall its libraries for this Python first" : !isReady(state.loaded) ? "Install its libraries first" : "Trust this exact revision";
  }
  const install = document.querySelector(".pp-install");
  if (install) { install.disabled = bundled || state.loadError || !state.loaded || state.dirty || state.installing; install.title = bundled ? "Built-in processors use libraries shipped with Workbench" : "Install or update libraries"; }
  for (const control of [document.querySelector(".pp-name"), document.querySelector(".pp-source"), document.querySelector(".pp-requirements")]) if (control) control.readOnly = bundled;
}
function setEditorValue(value) {
  const d = state.draft || (state.draft = { name: "New processor", source: TEMPLATE, requirements: "" });
  d.name = value?.name ?? d.name;
  d.source = value?.source ?? d.source;
  const requirements = value?.requirements ?? d.requirements;
  d.requirements = Array.isArray(requirements) ? requirements.join("\n") : String(requirements || "");
  const name = document.querySelector(".pp-name");
  const source = document.querySelector(".pp-source");
  const req = document.querySelector(".pp-requirements");
  if (name) name.value = d.name;
  if (source) {
    source.value = d.source;
    paintSourceHighlight(source);
  }
  if (req) req.value = d.requirements;
  updateCursor(source);
  state.dirty = false;
  updateEditorState();
  updateLockSummary();
}

async function refreshProcessors(selectId = state.selected, { preserveDirty = true } = {}) {
  let result;
  try { result = await postprocessors.list(); }
  catch (error) {
    state.processors = [];
    state.loadError = true;
    updateEditorState();
    updateLockSummary();
    repaintLibrary();
    repaintSimplePicker();
    patchRunForm();
    const box = document.querySelector(".pp-library-list");
    if (box) box.replaceChildren(el("div", { class: "empty" }, error.message || "Post-processing is not available yet."));
    return;
  }
  state.processors = normalizeList(result);
  state.loadError = false;
  const simple = uiMode() === "simple";
  const available = simple ? state.processors.filter(canRun) : state.processors;
  const keepDraft = !simple && preserveDirty && state.dirty;
  const prefillId = S.postprocessPrefill?.processor_id || null;
  const requested = prefillId || selectId;
  if (keepDraft) {
    // A refresh must never turn an unsaved new draft into an edit of the
    // first saved processor, or silently move an existing draft to a peer.
    state.selected = state.processors.some(p => p.id === state.selected) ? state.selected : null;
  } else {
    state.selected = available.some(p => p.id === requested)
      ? requested : (prefillId ? null : available[0]?.id || null);
    if (prefillId && !state.selected)
      toast("warn", "The processor used by this job is no longer available to run.");
  }
  if (state.selected && keepDraft) {
    const summary = selectedProcessor();
    state.loaded = state.loaded ? { ...state.loaded, ...summary } : summary;
    state.loadError = false;
  } else if (state.selected && simple) {
    state.loaded = selectedProcessor();
    state.loadError = false;
    state.dirty = false;
  } else if (state.selected) await loadProcessor(state.selected);
  else if (keepDraft) {
    state.loaded = null;
  } else {
    state.loaded = null;
    state.loadError = false;
    if (!simple) setEditorValue({ name: "New processor", source: TEMPLATE, requirements: "" });
  }
  updateEditorState();
  updateLockSummary();
  repaintLibrary();
  repaintSimplePicker();
  patchRunForm();
}

async function loadProcessor(id) {
  state.selected = id;
  const summary = selectedProcessor();
  if (!summary) return;
  try {
    const detail = await postprocessors.get(id);
    state.loaded = { ...summary, ...detail };
    state.loadError = false;
    setEditorValue({ name: state.loaded.name || "Processor", source: state.loaded.source || "", requirements: state.loaded.requirements || "" });
  } catch (error) { state.loaded = null; state.loadError = true; updateEditorState(); updateLockSummary(); toast("err", error.message || "Could not load processor."); }
}

function repaintLibrary() {
  const box = document.querySelector(".pp-library-list");
  if (!box) return;
  box.replaceChildren();
  if (!state.processors.length) { box.append(el("div", { class: "empty" }, "No processors saved yet. Create one below.")); return; }
  for (const p of state.processors) {
    const active = p.id === state.selected;
    const trust = isTrusted(p), ready = isReady(p);
    const actions = el("div", { class: "pp-actions" },
      el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: e => { e.stopPropagation(); duplicateProcessor(p); } }, "Duplicate"));
    if (!p.bundled) actions.append(el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: e => { e.stopPropagation(); deleteProcessor(p); } }, "Delete"));
    const meta = el("div", { class: "pp-processor-meta" });
    if (p.bundled) meta.append(el("span", { class: "chip" }, "Built in"));
    meta.append(el("span", { class: `chip ${trust ? "ok" : "warn"}` }, trust ? "Trusted" : "Untrusted"), el("span", { class: `chip ${ready ? "ok" : "warn"}` }, ready ? "Libraries ready" : isStale(p) ? "Libraries need reinstall" : (p.environment_status || "Libraries not ready")));
    box.append(el("div", { class: `pp-processor ${active ? "active" : ""}`, onclick: async () => {
      if (state.dirty && !await confirmModal({ title: "Discard unsaved changes?", text: "Your processor edits will be lost.", okLabel: "Discard changes", danger: true })) return;
      await loadProcessor(p.id); repaintLibrary(); patchRunForm();
    } },
      el("div", { class: "pp-processor-main" }, el("strong", {}, p.name || "Unnamed processor"), el("span", { class: "small faint mono" }, revision(p).slice(0, 12) || "no revision")),
      meta,
      actions,
    ));
  }
}

function repaintSimplePicker() {
  const select = document.querySelector(".pp-simple-select");
  if (!select) return;
  const ready = state.processors.filter(canRun);
  select.replaceChildren();
  if (!ready.length) {
    select.append(el("option", { value: "" }, "No ready processors"));
    select.disabled = true;
  } else {
    for (const p of ready) select.append(el("option", { value: p.id }, p.name || "Unnamed processor"));
    select.disabled = false;
    select.value = state.selected || ready[0].id;
  }
  const detail = document.querySelector(".pp-simple-detail");
  const selected = selectedProcessor();
  if (detail) detail.textContent = !selected ? "No processor is ready to run." : selected.bundled ? "Built into Workbench: enlarges each image to 4× its width and height using high-quality Lanczos resampling. JPEG and PNG output is always set to 1200 DPI; the source DPI is not multiplied." : "This processor was installed and trusted in Advanced mode.";
}

async function showGuide() {
  const root = $("#modal-root");
  const modal = $(".modal", root);
  const backdrop = $(".modal-backdrop", root);
  let open = true;
  const onKey = event => { if (event.key === "Escape") close(); };
  const close = () => {
    if (!open) return;
    open = false;
    root.hidden = true;
    modal.classList.remove("pp-guide-modal");
    backdrop.onclick = null;
    document.removeEventListener("keydown", onKey);
  };
  modal.classList.add("pp-guide-modal");
  modal.replaceChildren(
    el("div", { class: "m-ico info" }, ico("book")),
    el("h3", {}, "Image post-processing guide"),
    el("div", { class: "m-loading" }, "Loading the guide bundled with this version…"),
  );
  root.hidden = false;
  backdrop.onclick = close;
  document.addEventListener("keydown", onKey);
  try {
    const result = await postprocessors.guide();
    if (!open) return;
    modal.replaceChildren(el("h3", {}, result.title || "Image post-processing guide"));
    modal.append(el("p", { class: "m-when" }, `Bundled with SCM Workbench v${result.version || "unknown"}`));
    const body = el("div", { class: "notes pp-guide-content" });
    // The server reads the version-bundled Markdown through a bounded regular
    // file and emits only its small escaped HTML subset.
    body.innerHTML = result.body || "<p>(The bundled guide is empty.)</p>";
    modal.append(body, el("div", { class: "m-actions" }, el("button", { class: "btn primary", type: "button", onclick: close }, "Close")));
  } catch (error) {
    if (!open) return;
    modal.replaceChildren(
      el("div", { class: "m-ico warn" }, ico("alert")),
      el("h3", {}, "Guide unavailable"),
      el("p", {}, error?.message || "The bundled image post-processing guide could not be loaded."),
      el("div", { class: "m-actions" }, el("button", { class: "btn", type: "button", onclick: close }, "Close")),
    );
  }
}

async function newProcessor() {
  if (state.dirty && !await confirmModal({ title: "Discard unsaved changes?", text: "Your processor edits will be lost.", okLabel: "Discard changes", danger: true })) return;
  state.selected = null;
  state.loaded = null;
  state.loadError = false;
  state.draft = { name: "New processor", source: TEMPLATE, requirements: "" };
  setEditorValue(state.draft);
  repaintLibrary();
  patchRunForm();
}

async function duplicateProcessor(p) {
  try { await postprocessors.duplicate(p.id, `${p.name || "Processor"} copy`, revision(p)); await refreshProcessors(); toast("ok", "Processor duplicated."); }
  catch (error) { toast("err", error.message || "Could not duplicate processor."); }
}
async function deleteProcessor(p) {
  if (!await confirmModal({ title: "Delete processor?", text: `Delete “${p.name || "processor"}” and its saved revisions?`, okLabel: "Delete processor", danger: true })) return;
  try {
    await postprocessors.delete(p.id, revision(p));
    if (p.id === state.selected) {
      state.selected = null; state.loaded = null; state.dirty = false;
      await refreshProcessors(null, { preserveDirty: false });
    } else {
      await refreshProcessors(state.selected, { preserveDirty: true });
    }
    toast("ok", "Processor deleted.");
  } catch (error) { toast("err", error.message || "Could not delete processor."); }
}

async function saveRevision() {
  const d = state.draft;
  if (!d?.name?.trim() || !d.source) return toast("warn", "Enter a processor name and source first.");
  if (sourceBytes(d.source) > 256 * 1024) return toast("err", "Processor source is too large.");
  try {
    const result = await postprocessors.save({ processor_id: state.selected, name: d.name.trim(), source: d.source, requirements: d.requirements, expected_revision: revision(state.loaded) || null });
    await refreshProcessors(result?.processor?.id || result?.id || state.selected, { preserveDirty: false });
    toast("ok", "Saved an untrusted revision. Trust it before running.");
  } catch (error) { toast("err", error.message || "Could not save the processor."); }
}

async function importSource() {
  try {
    const result = await postprocessors.importSource({ saveDraft: payload => postprocessors.save(payload) });
    if (result === null) return;
    if (result?.ok === false) throw new Error((result.errors || ["Import failed."])[0]);
    const id = result?.processor?.id || result?.id;
    if (id) await refreshProcessors(id, { preserveDirty: false }); else setEditorValue(result);
    state.dirty = false;
    toast("ok", "Imported an untrusted revision. Trust it before running.");
  } catch (error) { toast("err", error.message || "Could not import the processor."); }
}
async function revert() {
  if (!state.loaded) {
    if (!state.dirty || await confirmModal({ title: "Revert unsaved changes?", text: "Your current editor contents will be replaced.", okLabel: "Revert changes", danger: true }))
      setEditorValue({ name: "New processor", source: TEMPLATE, requirements: "" });
    return;
  }
  const revisions = Array.isArray(state.loaded.revisions) ? state.loaded.revisions : [];
  const activeRevision = revision(state.loaded);
  const picker = el("select", { class: "input pp-revision-picker", "aria-label": "Saved processor revision" });
  for (const item of revisions) {
    const saved = new Date(Number(item.saved_at) * 1000);
    const when = Number.isFinite(saved.getTime()) ? saved.toLocaleString() : "Saved revision";
    picker.append(el("option", { value: item.revision, selected: item.revision === activeRevision },
      `${item.revision.slice(0, 12)} · ${when}${item.active ? " · current" : ""}`));
  }
  if (!revisions.length) picker.append(el("option", { value: activeRevision }, `${activeRevision.slice(0, 12)} · current`));
  const approved = await confirmModal({
    title: "Revert to a saved revision?",
    text: "Choose the immutable revision to load. Your current editor contents will be replaced. Loading an older revision does not make it active until you save it.",
    content: el("label", {}, "Saved revision", picker),
    okLabel: "Load revision",
    danger: true,
  });
  if (!approved) return;
  const chosen = picker.value;
  if (chosen === activeRevision) {
    setEditorValue(state.loaded);
    return;
  }
  try {
    const historical = await postprocessors.get(state.loaded.id, chosen);
    setEditorValue({ ...historical, name: state.loaded.name });
    state.dirty = true;
    updateEditorState();
    updateLockSummary();
    toast("ok", `Loaded revision ${chosen.slice(0, 12)}. Save it to make it current.`);
  } catch (error) { toast("err", error.message || "Could not load the saved revision."); }
}
async function trustRevision() {
  const p = state.loaded || selectedProcessor();
  if (!p) return toast("warn", "Save a processor before trusting it.");
  const approved = await confirmModal({
    title: "Trust this Python revision?",
    text: "This code and its installed libraries run as your user account and can access your files and network. Only continue if you trust the exact source and packages shown here.",
    okLabel: "Trust revision",
    danger: true,
  });
  if (!approved) return;
  try { await postprocessors.trust(p.id, revision(p), p.environment_fingerprint || p.environment?.fingerprint || null); await refreshProcessors(p.id); toast("ok", "This exact revision is trusted."); }
  catch (error) { toast("err", error.message || "Could not trust this revision."); }
}
async function installLibraries() {
  const p = state.loaded || selectedProcessor();
  if (!p) return toast("warn", "Save a processor first.");
  if (state.dirty) return toast("warn", "Save the requirements as a new revision before installing them.");
  const requirements = String(state.draft?.requirements || "").trim();
  const approved = await confirmModal({
    title: "Install processor libraries?",
    text: requirements ? `Workbench will download wheel packages for:\n\n${requirements}` : "This processor has no additional libraries. Workbench will prepare its empty environment.",
    okLabel: "Install libraries",
  });
  if (!approved) return;
  try {
    const job = await doRun("postprocess_dependencies", null, { args: { processor_id: p.id, revision_hash: revision(p), requirements } });
    if (job?.id) { state.installing = true; updateEditorState(); watchJobDone(job.id, () => refreshProcessors(p.id)); }
  } catch (error) { toast("err", error.message || "Could not start library installation."); }
}

function paintRunGate() {
  const scope = first(formArgs("postprocess_images")?.scope) || "both";
  const countKnown = state.imageScope === scope && Number.isInteger(state.imageCount);
  const p = selectedProcessor();
  const running = state.job && (S.jobs || []).some(job => job.id === state.job.id && job.status === "running");
  const gate = running ? "Processor job is running" : !p ? "Choose a processor" : state.loadError ? "Could not verify the selected revision" : !isReady(p) ? (isStale(p) ? "Reinstall libraries for this Python first" : "Install or update libraries first") : !isTrusted(p) ? "Trust this exact revision first" : !countKnown ? "Checking image inventory…" : state.imageCount === 0 ? "No recognized images in this scope" : "Ready to run";
  const run = document.querySelector(".pp-run");
  if (run) { run.disabled = gate !== "Ready to run"; run.title = gate; }
  const note = document.querySelector(".pp-run-note");
  if (note) note.textContent = countKnown && state.imageCount > 0 ? `${gate} · ${state.imageCount} recognized image${state.imageCount === 1 ? "" : "s"}` : gate;
}

function patchRunForm() {
  const kind = "postprocess_images";
  const card = document.querySelector(`.form-card[data-kind="${kind}"]`);
  if (!card) return;
  const args = formArgs(kind);
  if (S.postprocessPrefill?.scope && args.scope !== undefined) args.scope = S.postprocessPrefill.scope;
  S.postprocessPrefill = null;
  args.processor_id = state.selected || "";
  args.revision_hash = revision(selectedProcessor());
  const scope = first(args.scope) || "both";
  if (state.imageScope !== scope) state.imageCount = null;
  afterFormChange(kind, args);
  paintRunGate();
}

function attachRunStatus(root) {
  const status = el("div", { class: "pp-run-status", "aria-live": "polite", hidden: true });
  const host = $(".pp-run-card", root) || root;
  host.append(status);
  let stoppingId = null;
  const requestCancel = async jobId => {
    if (stoppingId === jobId) return;
    stoppingId = jobId;
    paint();
    try {
      const result = await jobs.kill(jobId);
      if (result?.ok) toast("warn", "Stopping…");
      else {
        if (stoppingId === jobId) stoppingId = null;
        toast("warn", "The job has already finished.");
      }
    } catch (error) {
      if (stoppingId === jobId) stoppingId = null;
      toast("err", error?.message || "Could not stop the processor job.");
    }
    paint();
  };
  const paint = () => {
    const running = state.job && (S.jobs || []).find(j => j.id === state.job.id);
    if (!running) { status.hidden = true; status.replaceChildren(); return; }
    status.hidden = false;
    const outcome = running.postprocess_outcome;
    const message = running.status === "running" ? "Processing images…" : running.status === "ok" ? (outcome === "unchanged" ? "Processing complete. Every result was byte-identical, so original files were left unchanged." : "Processing complete. Original images were replaced after validation.") : outcome === "needs_attention" ? "Processing failed and rollback could not be verified. Inspect the image folders and job details before continuing." : "Processing failed or was cancelled. Original images were not changed.";
    const panel = el("div", { class: `pp-status ${running.status}` }, el("div", {}, message));
    status.replaceChildren(panel);
    if (running.status === "running") {
      const done = Number(running.progress?.current || 0), total = Number(running.progress?.total || running.image_total || 0);
      panel.append(el("div", { class: "pp-progress" }, el("progress", { max: total || 1, value: Math.min(done, total || 1) }), el("span", { class: "small faint" }, total ? `${done} / ${total}` : "Preparing image set…")));
      if (uiMode() === "simple") panel.append(el("button", {
        class: "btn btn-ghost btn-sm pp-cancel", type: "button",
        disabled: stoppingId === running.id,
        onclick: () => requestCancel(running.id),
      }, stoppingId === running.id ? "Stopping…" : "Cancel processing"));
    } else if (running.status === "ok") status.append(el("button", { class: "btn primary", onclick: () => go("pdf") }, ico("arrow"), "Go to Create PDF"));
  };
  const previewListener = event => {
    const detail = event.detail || {};
    if (detail.kind !== "postprocess_images") return;
    state.imageScope = first(detail.args?.scope) || "both";
    state.imageCount = Number.isInteger(detail.result?.image_count) ? detail.result.image_count : null;
    paintRunGate();
  };
  document.addEventListener(COMMAND_PREVIEW_EVENT, previewListener);
  let lastDependencyStatus = "";
  const tick = async () => { try {
    const result = await jobs.list();
    S.jobs = result.jobs || S.jobs;
    const processing = (S.jobs || []).find(j => j.kind === "postprocess_images" && j.status === "running");
    if (processing) state.job = processing;
    const dependency = (S.jobs || []).find(j => j.kind === "postprocess_dependencies" && j.status === "running");
    state.installing = !!dependency;
    updateEditorState();
    if (!dependency && lastDependencyStatus === "running") await refreshProcessors(state.selected);
    lastDependencyStatus = dependency ? "running" : "idle";
    paint();
    paintRunGate();
  } catch {} };
  state.timer = setInterval(tick, 700); tick();
  root.__dispose = () => { clearInterval(state.timer); document.removeEventListener(COMMAND_PREVIEW_EVENT, previewListener); if (state.sub) state.sub.close(); state.sub = null; state.timer = null; if (S.pageGuard === root.__guard) S.pageGuard = null; };
}

function renderSimplePostprocess() {
  const wrap = el("div", {});
  wrap.append(pageHead("Image post-processing", "Optionally improve fetched card images before creating your PDF."));
  wrap.append(el("div", { class: "banner info" }, ico("sparkle"), el("span", {}, "Simple mode only shows processors that are already installed and trusted. Processor code, trust, and libraries are managed in Advanced mode.")));
  wrap.append(el("section", { class: "card pp-simple-picker" },
    el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("layers")), el("div", { class: "grow" }, el("h2", {}, "Choose an image processor"), el("p", {}, "The built-in Simple Upscaler is ready without any downloads."))),
    el("label", {}, "Processor", el("select", { class: "input pp-simple-select", "aria-label": "Ready image processor" })),
    el("div", { class: "small faint pp-simple-detail" }, "Loading ready processors…")));
  const run = el("section", { class: "card pp-run-card" },
    el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("play")), el("div", { class: "grow" }, el("h2", {}, "Run processor"), el("p", {}, "Choose which fetched images to improve. Originals are replaced only after every result passes validation."))),
    formCard("postprocess_images", { run: false }),
    el("div", { class: "runbar" }, el("span", { class: "rb-note pp-run-note" }, "Checking image inventory…"), el("button", { class: "btn primary pp-run", type: "button", onclick: async e => { state.job = await doRun("postprocess_images", e.currentTarget); paintRunGate(); } }, ico("play"), "Run processor")));
  wrap.append(run);
  wrap.__patch = async () => {
    state.dirty = false;
    S.pageGuard = null;
    $(".pp-simple-select", wrap).addEventListener("change", event => {
      state.selected = event.currentTarget.value || null;
      state.loaded = selectedProcessor();
      patchRunForm();
      repaintSimplePicker();
    });
    $(".pp-run-card", wrap).addEventListener("change", () => { const scope = first(formArgs("postprocess_images")?.scope) || "both"; if (state.imageScope !== scope) state.imageCount = null; paintRunGate(); });
    await refreshProcessors(state.selected, { preserveDirty: false });
    repaintSimplePicker();
    patchRunForm();
    attachRunStatus(wrap);
  };
  return wrap;
}

PAGES.postprocess = root => {
  if (uiMode() === "simple") return renderSimplePostprocess();
  const wrap = el("div", {});
  wrap.append(pageHead("Image post-processing", "Save a trusted Python processor, install its optional libraries, then apply it manually to fetched card images."));
  wrap.append(el("div", { class: "banner warn pp-warning" }, ico("alert"), el("span", {}, "Python processors and their libraries run as your user account. Only use code and packages you trust. Workbench limits inputs, resources, and image publication, but it cannot safely sandbox arbitrary Python from your other files or network.")));
  const library = el("section", { class: "card pp-library" }, el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("layers")), el("div", { class: "grow" }, el("h2", {}, "Processor library"), el("p", {}, "Select a revision to edit, trust, install, or run.")), el("div", { class: "actions" }, el("button", { class: "btn btn-ghost pp-guide", type: "button", "aria-label": "Open the image post-processing guide", onclick: showGuide }, ico("book"), "Guide"), el("button", { class: "btn btn-ghost", type: "button", onclick: newProcessor }, "New processor"))), el("div", { class: "pp-library-list" }));
  wrap.append(library);
  const sourceEditor = el("div", { class: "pp-source-wrap" },
    el("pre", { class: "pp-source-highlight", "aria-hidden": "true" }, el("code", { class: "pp-source-code" })),
    el("textarea", { class: "input pp-source", rows: 16, wrap: "off", spellcheck: "false", autocomplete: "off", autocapitalize: "off", "aria-label": "Python source" }));
  const editor = el("section", { class: "card pp-editor" }, el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("file")), el("div", { class: "grow" }, el("h2", {}, "Processor editor"), el("p", {}, "Saving creates an immutable, untrusted revision.")), el("span", { class: "pp-dirty" }, "Saved revision")),
    el("label", {}, "Processor name", el("input", { class: "input pp-name", spellcheck: "false" })),
    el("label", {}, "Python source", sourceEditor),
    el("label", {}, "Optional requirements", el("textarea", { class: "input pp-requirements", rows: 4, spellcheck: "false", placeholder: "Pillow==10.4.0" })),
    el("div", { class: "pp-lock" }, el("span", { class: "small faint" }, "No third-party wheels are required.")),
    el("div", { class: "small faint mono pp-cursor" }, "Line 1, column 1"),
    el("div", { class: "runbar pp-editor-actions" }, el("span", { class: "rb-note" }, "Source is parsed when saved, never executed."), el("button", { class: "btn btn-ghost", type: "button", onclick: importSource }, "Import .py"), el("button", { class: "btn btn-ghost", type: "button", onclick: revert }, "Revert"), el("button", { class: "btn btn-ghost pp-trust", type: "button", onclick: trustRevision }, "Trust this revision"), el("button", { class: "btn btn-ghost pp-install", type: "button", onclick: installLibraries }, "Install / update libraries"), el("button", { class: "btn primary pp-save", type: "button", onclick: saveRevision }, "Save revision")));
  wrap.append(editor);
  const run = el("section", { class: "card pp-run-card" }, el("div", { class: "card-head" }, el("div", { class: "card-ico" }, ico("play")), el("div", { class: "grow" }, el("h2", {}, "Run processor"), el("p", {}, "One isolated batch processes the selected image scope."))), formCard("postprocess_images", { run: false }), el("div", { class: "runbar" }, el("span", { class: "rb-note pp-run-note" }, "Choose a processor"), el("button", { class: "btn primary pp-run", type: "button", onclick: async e => { state.job = await doRun("postprocess_images", e.currentTarget); paintRunGate(); } }, ico("play"), "Run processor")));
  wrap.append(run);
  wrap.__patch = async () => {
    if (!state.draft) state.draft = { name: "New processor", source: TEMPLATE, requirements: "" };
    const source = $(".pp-source", wrap), req = $(".pp-requirements", wrap), name = $(".pp-name", wrap);
    source.oninput = () => { state.draft.source = source.value; markDirty(); updateCursor(source); paintSourceHighlight(source); };
    for (const event of ["click", "keyup", "select"]) source.addEventListener(event, () => updateCursor(source));
    source.addEventListener("scroll", () => syncSourceHighlightScroll(source));
    req.oninput = () => { state.draft.requirements = req.value; markDirty(); };
    name.oninput = () => { state.draft.name = name.value; markDirty(); };
    source.addEventListener("keydown", e => { if (e.key === "Tab" && !e.target.readOnly) { e.preventDefault(); const at = e.target.selectionStart; e.target.setRangeText("    ", at, e.target.selectionEnd, "end"); state.draft.source = e.target.value; markDirty(); updateCursor(source); paintSourceHighlight(source); } });
    $(".pp-run-card", wrap).addEventListener("change", () => { const scope = first(formArgs("postprocess_images")?.scope) || "both"; if (state.imageScope !== scope) state.imageCount = null; paintRunGate(); });
    await refreshProcessors(state.selected, { preserveDirty: false }); repaintLibrary(); patchRunForm(); attachRunStatus(wrap);
  };
  wrap.__guard = async () => {
    if (!state.dirty) return true;
    const leave = await confirmModal({ title: "Unsaved processor changes", text: "Leave this page and discard your edits?", okLabel: "Leave page", danger: true });
    if (leave) state.dirty = false;
    return leave;
  };
  S.pageGuard = wrap.__guard;
  return wrap;
};
