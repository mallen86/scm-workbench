/* jobstrip — the simple-mode stand-in for the job console.
   One per page (fetch, create pdf): while a job of the page's kind runs, the
   strip shows a progress bar — a sliding indeterminate bar by default, but
   driven by REAL numbers when the script reports them ("Fetched 42/101
   images", the fetch plugins' per-batch progress line): the bar fills to
   the actual fraction and the label counts x/y. When the job settles, a
   success or failure line appears with the page's follow-up action (fetch →
   "go create the PDF", create pdf → "open the PDF"). Advanced mode never
   sees the strip — the console drawer is that page's status there instead. */

import { $, S, el, ico, toast } from "./core.js";import { jobs } from "./jobs.js";import { uiMode } from "./nav.js";
export function jobStrip(kind, opts = {}) {
  const strip = el("div", { class: "jobstrip", hidden: true });
  const label = el("div", { class: "js-label" }, "");
  const bar = el("div", { class: "js-bar" }, el("i", {}));
  const body = el("div", { class: "js-body" });
  strip.append(
    el("div", { class: "js-top" }, el("span", { class: "js-ico" }, ico(opts.icon || "play")), label),
    bar,
    body,
  );

  // A timestamp remains the fallback for callers outside doRun(). Jobs
  // started by this UI are also remembered in S.startedJobIds, so navigating
  // away while one runs and returning after it finishes still shows its
  // completion actions without reviving a job from an earlier app session.
  const t0 = Date.now() / 1000 - 1;

  // ---- live progress from the job's own stream ---------------------------
  // The console drawer is closed in simple mode, so nothing else consumes the
  // job's SSE stream; the strip opens one on the running job and harvests
  // progress from it. Any line containing "n/m" (the fetch plugins print
  // "Fetched 42/101 images" per batch) moves the bar to the real fraction.
  // If no such line ever arrives the bar keeps its indeterminate slide.
  let subscription = null, esJobId = null, lastX = 0, lastY = 0;
  let imageDone = 0, imageTotal = 0;
  const closeEs = () => { if (subscription) { try { subscription.close(); } catch {} subscription = null; } };

  const attachProgress = (job) => {
    if (subscription && esJobId === job.id) return;
    closeEs();
    esJobId = job.id;
    lastX = 0; lastY = 0; imageDone = 0; imageTotal = 0;
    const showProgress = (x, y) => {
      if (y <= 0 || x > y || (y === lastY && x < lastX)) return;
      lastX = x; lastY = y;
      if (!strip.isConnected) return;
      const pct = Math.min(100, Math.round(x / y * 100));
      bar.style.setProperty("--pct", pct + "%");
      strip.classList.add("prog");
      label.textContent = `${opts.runningLabel || "Working"}  ·  ${x}/${y} (${pct}%)`;
    };
    if (opts.progressTotal) {
      Promise.resolve(opts.progressTotal(job)).then(total => {
        if (esJobId !== job.id) return;
        imageTotal = Number(total) || 0;
        if (imageDone && imageTotal) showProgress(imageDone, imageTotal);
      }).catch(() => {}); // an unknown total deliberately leaves the cycling bar
    }
    subscription = jobs.subscribe(job.id, {
      after: 0,
      onLine: d => {
        const fraction = /(\d+)\s*\/\s*(\d+)/.exec(d.s);
        if (fraction) return showProgress(+fraction[1], +fraction[2]);
        const image = /^\s*Image\s+(\d+)\s*:/i.exec(d.s);
        if (!image) return;
        imageDone = Math.max(imageDone, +image[1]);
        if (imageTotal) showProgress(imageDone, imageTotal);
      },
      onDone: d => {
        // Refresh the canonical row before painting: the terminal stream event
        // has status but not output paths, and those paths power “Open PDF”.
        const id = esJobId;
        const j = (S.jobs || []).find(x => x.id === id);
        if (j) { j.status = d.status; j.exit_code = d.exit_code; }
        closeEs(); esJobId = null;
        if (!id) return;
        jobs.list().then(result => {
          S.jobs = result.jobs || S.jobs;
          paint();
        }).catch(() => paint());
      },
      onError: error => { if (strip.isConnected) toast("warn", `Job output: ${error.message || error}`); },
      onGap: () => { if (strip.isConnected) toast("warn", "Some earlier job output was truncated."); },
    });
  };

  const tail = async (id) => {
    try {
      const d = await jobs.log(id);
      const lines = (d.lines || []).filter(l => l.trim() && !/^\($/.test(l.trim()));
      return lines.slice(-2).join("  ·  ");
    } catch { return ""; }
  };

  let painting = false;
  const paint = async () => {
    if (painting) return;
    painting = true;
    try {
      if (uiMode() !== "simple") { strip.hidden = true; closeEs(); return; }
      if (!strip.isConnected) return;          // page gone — the timer cleans up below
      const jobs = (S.jobs || []).filter(j => j.kind === kind);
      const run = jobs.find(j => j.status === "running");
      if (run) {
        strip.dataset.painted = "";               // a new run may re-render onOk later
        attachProgress(run);
        strip.hidden = false;
        strip.className = strip.classList.contains("prog") ? "jobstrip running prog" : "jobstrip running";
        if (!strip.classList.contains("prog"))
          label.textContent = (opts.runningLabel || "Working") + "  ·  started " +
            new Date((run.ts || Date.now() / 1000) * 1000).toLocaleTimeString();
        body.innerHTML = "";
        return;
      }
      closeEs();
      const remembered = S.startedJobIds?.[kind];
      const done = (remembered && jobs.find(j => j.id === remembered)) || jobs.find(j => j.ts >= t0);
      if (!done) { strip.hidden = true; strip.className = "jobstrip"; return; }
      // same job already painted? don't tear the body down and re-run onOk
      // every 2 s (it re-appends buttons and would restart any async work).
      const key = done.id + ":" + done.status;
      if (strip.dataset.painted === key) { strip.hidden = false; return; }
      strip.dataset.painted = key;
      strip.hidden = false;
      body.innerHTML = "";
      if (done.status === "ok") {
        strip.className = "jobstrip done";
        label.textContent = "Done";
        if (opts.onOk) opts.onOk(done, body);
        else body.append(el("div", { class: "js-msg ok" }, ico("check"), el("span", {}, "Job finished successfully.")));
      } else {
        strip.className = "jobstrip failed";
        label.textContent = done.status === "killed" ? "Stopped" : "Failed";
        body.append(el("div", { class: "js-msg err" },
          ico("x"), el("span", {}, done.status === "killed" ? "You stopped this job." : "The job didn't finish — the output below usually says why.")));
        const t = await tail(done.id);
        if (t && strip.isConnected) body.append(el("div", { class: "js-tail" }, t));
        body.append(el("div", { class: "js-hint" }, "Fix the form above and run it again."));
      }
    } finally {
      painting = false;
    }
  };

  // repaint while the card is around: the 2 s poll catches start/stop, the
  // SSE stream catches live progress in between. Per-kind timer so two strips
  // can never clear each other's interval.
  const tk = "jobstrip-" + kind;
  const tick = () => {
    if (!strip.isConnected) {
      clearInterval(S.timers[tk]);
      S.timers[tk] = null;
      closeEs();
      return;
    }
    paint();
  };
  clearInterval(S.timers[tk]);
  S.timers[tk] = setInterval(tick, 500);
  paint();
  // Navigation can rebuild this strip between the terminal event and the
  // next global jobs poll. Fetch the canonical row immediately so completed
  // output actions appear after one local request, not a multi-second timer.
  jobs.list().then(result => {
    if (!strip.isConnected) return;
    S.jobs = result.jobs || S.jobs;
    paint();
  }).catch(() => {});

  return strip;
}
