/* job-history: the shared list behind the dedicated Job history page.
   Rows are built here rather than in the page module so the jobs poll can
   repaint the list in place while the page stays mounted, and so the
   "restore this job's settings" action has one definition in both interface
   modes. The page itself (pages/history.js) only lays out the cards. */

import { S, el, ico, toast } from "./core.js";
import { displayCmd } from "./forms.js";
import { go, uiMode, SIMPLE_PAGES } from "./nav.js";

/* =========================== job -> page mapping ========================== */

/* Every manifest kind names the page that owns its settings, so a job can
   always be turned back into "that page, with these settings". Kinds with no
   manifest entry (the app self-update) have no page to restore. */
export function jobPageFor(kind) {
  const spec = kind ? S.manifest?.[kind] : null;
  return spec && spec.page ? spec.page : null;
}


export function jobRestorable(job) {
  return !!jobPageFor(job?.kind);
}


/* The prefill `go()` understands: the kind whose form to fill and the exact
   args to filter through the manifest. A fetch job also carries its game so
   the page opens on the same plugin. */
export function jobPrefill(job) {
  const page = jobPageFor(job?.kind);
  if (!page) return null;
  const prefill = { kind: job.kind, args: job.args || {} };
  if (job.kind.startsWith("fetch:")) prefill.plugin = job.kind.slice("fetch:".length);
  return { page, prefill };
}


const KIND_ICONS = [
  [/^fetch:/, "download"],
  [/^create_pdf$/, "pdf"],
  [/^(offset_pdf|calibration)$/, "target"],
  [/^dxf_/, "scissors"],
  [/^extras_/, "sparkle"],
  [/^clean_up$/, "trash"],
  [/^(repo_init|repo_update)$/, "refresh"],
  [/^update$/, "refresh"],
];


export function jobIcon(kind) {
  const hit = KIND_ICONS.find(([re]) => re.test(kind || ""));
  return hit ? hit[1] : "terminal";
}


/* Time for a list that spans sessions: the clock for today's jobs, the date
   and clock for anything older. */
export function fmtJobTs(t) {
  const secs = Number(t);
  if (!Number.isFinite(secs) || secs <= 0) return "";
  const d = new Date(secs * 1000);
  if (!Number.isFinite(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}


/* ================================ actions ================================= */

/* Restore a job's settings into its page and go there. Simple mode keeps its
   short page list; a job whose page is not offered there explains itself
   instead of silently doing nothing. */
export function openJobSettings(job) {
  const target = jobPrefill(job);
  if (!target) return;
  if (uiMode() === "simple" && !SIMPLE_PAGES.includes(target.page)) {
    toast("warn", "This job's settings live on a page that Simple mode hides. Switch to Advanced to open them.");
    return;
  }
  go(target.page, target.prefill);
  toast("ok", `Loaded the settings “${job.title}” ran with.`);
}


async function openJobLog(job) {
  const { openConsole } = await import("./console.js");
  openConsole(job.id);
}


/* ================================== rows ================================== */

export function jobHistoryRow(job) {
  const restorable = jobRestorable(job);
  return el("div", {
    class: `jobrow hist${restorable ? " restorable" : ""}`,
    "data-kind": job.kind,
    title: restorable ? "Open this job's page with the settings it ran with" : job.title,
    onclick: () => { if (restorable) openJobSettings(job); },
  },
    el("div", { class: "jr-ico" }, ico(jobIcon(job.kind))),
    el("div", { class: "jr-body" },
      el("div", { class: "jr-t" }, job.title),
      el("div", { class: "jr-cmd" }, displayCmd(job.cmd, job.kind) || ""),
    ),
    el("div", { class: "jr-meta" },
      el("div", { class: `statusdot ${job.status}` }, job.status),
      el("div", { class: "t" }, fmtJobTs(job.ts)),
    ),
    el("div", { class: "jr-act" },
      // the console only exists in advanced mode; the guard is the CSS class
      // (a mode switch is a pure layout change) and openConsole() is a no-op in
      // simple mode, so the row never has to be rebuilt for a mode flip
      el("button", {
        class: "btn btn-ghost btn-sm", type: "button", title: "Show this job's output",
        onclick: e => { e.stopPropagation(); openJobLog(job); },
      }, ico("terminal"), el("span", { class: "btn-label" }, "Log")),
      restorable ? el("span", { class: "jr-go" }, ico("arrow")) : null,
    ),
  );
}


function section(title, jobs, emptyText) {
  const card = el("div", { class: "card" },
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("clock")),
      el("div", { class: "grow" }, el("h2", {}, title),
        el("p", {}, `${jobs.length} job${jobs.length === 1 ? "" : "s"}`)),
    ),
  );
  if (!jobs.length) card.append(el("div", { class: "empty" }, ico("clock"), emptyText));
  else for (const j of jobs) card.append(jobHistoryRow(j));
  return card;
}


/* Repaint the list in place. Called by refreshJobs() while the page is up, so
   a job that starts or finishes while the user is looking at history updates
   without a full re-render. */
export function renderJobHistory(slot) {
  if (!slot) return;
  const jobs = S.jobs || [];
  slot.innerHTML = "";
  const running = jobs.filter(j => j.status === "running");
  const done = jobs.filter(j => j.status !== "running");
  if (running.length) slot.append(section("Running now", running, "Nothing is running."));
  slot.append(section("Finished", done, "Finished jobs will be listed here."));
}
