/* Bounded sidebar notices for ordinary jobs. Repository and app updates already
   own richer progress boxes, so this module handles the remaining job kinds. */

import { $, el, ico, S } from "./core.js";
import { createJobNoticeState } from "./job-notice-state.js";
import { jobNoticeProgress } from "./job-notice-progress.js";

const state = createJobNoticeState();
let expiryTimer = null;
let renderedSignature = null;


function heading(status) {
  if (status === "running") return "Job running";
  if (status === "ok") return "Job finished";
  if (status === "killed") return "Job stopped";
  return "Job failed";
}


function clearRendered() {
  document.querySelectorAll("[data-job-notice]").forEach(node => node.remove());
}


function scheduleExpiry(records) {
  if (expiryTimer) clearTimeout(expiryTimer);
  expiryTimer = null;
  const next = records.reduce((soonest, record) => record.expiresAt && (!soonest || record.expiresAt < soonest)
    ? record.expiresAt : soonest, 0);
  if (!next) return;
  expiryTimer = setTimeout(() => syncJobNotices(S.jobs), Math.max(0, next - Date.now()) + 5);
}


function render(records) {
  const foot = $(".sidebar-foot");
  const views = records.map(record => ({ record, progress: jobNoticeProgress(record.job, record.status) }));
  const signature = views.map(({ record, progress }) =>
    `${record.job.id}:${record.status}:${record.job.title || ""}:${progress.text}:${progress.fraction ?? ""}`).join("|");
  if (!foot) { clearRendered(); renderedSignature = null; return; }
  if (signature === renderedSignature && document.querySelectorAll("[data-job-notice]").length === records.length) {
    scheduleExpiry(records);
    return;
  }

  clearRendered();
  for (const { record, progress } of views) {
    const head = el("div", { class: "rp-head rp-head-row" }, el("span", {}, heading(record.status)));
    const close = el("button", { type: "button", class: "btn sm ghost", title: "Dismiss this job message",
      "aria-label": "Dismiss this job message" }, ico("x"));
    close.onclick = () => {
      state.dismiss(record.job.id);
      renderedSignature = null;
      syncJobNotices(S.jobs);
    };
    head.append(close);
    const row = el("div", { class: "rp-row" },
      el("div", { class: "rp-label" }, record.job.title || "Workbench job"),
      el("div", { class: "rp-meta" }, progress.text));
    const children = [head, row];
    if (record.status === "running") {
      const fill = el("div", { class: "rp-fill" });
      if (progress.fraction !== null) fill.style.width = `${progress.fraction * 100}%`;
      children.push(el("div", { class: `rp-bar${progress.fraction === null ? " indet" : ""}`, "aria-hidden": "true" }, fill));
    }
    const notice = el("div", { class: `repoprog sidebar-note job-notice ${record.status}`,
      "data-job-notice": record.job.id, "aria-live": "polite" }, children);
    foot.before(notice);
  }
  renderedSignature = signature;
  scheduleExpiry(records);
}


export function syncJobNotices(jobs = S.jobs) {
  render(state.sync(jobs));
}
