/* pages/preparing — the packaged first-launch repository setup screen. */

import { PAGES, S, el, ico, toast } from "../core.js";
import { bootPage, go } from "../nav.js";
import { doRun } from "../forms.js";
import { refreshInfo } from "../info.js";
import { startPrepWatcher, updatePrepRows } from "../prep.js";

function leaveSetup() {
  // Session-local by design: this screen is offered once at app boot, while
  // the prep watcher and repo-gated controls continue working after dismissal.
  S.firstBootDismissed = true;
  bootPage();
}

let retryPending = false;
async function retrySetup(button) {
  if (retryPending) return;
  const missing = (S.info?.repos || []).filter(r => !r.deployed);
  if (!missing.length) return;
  retryPending = true;
  button.disabled = true;
  button.replaceChildren(el("span", { class: "spinner" }), " Retrying…");
  const started = [];
  for (const row of missing) {
    const job = await doRun("repo_init", null, { args: { repo: row.key } });
    if (job) started.push(job);
  }
  if (!started.length) {
    retryPending = false;
    button.disabled = false;
    button.replaceChildren(ico("refresh"), "Retry setup");
    return;
  }
  toast("ok", `Retrying ${started.length} managed repositor${started.length === 1 ? "y" : "ies"}.`);
  await refreshInfo({ keepForms: true, jobs: false }).catch(() => {});
  retryPending = false;
  go("preparing", null, { push: false, anim: false });
  startPrepWatcher();
}

PAGES.preparing = () => {
  const repos = S.info?.repos || [];
  const active = !!S.info?.server?.active || repos.some(r => r.progress) ||
    (S.jobs || []).some(j => (j.kind === "repo_init" || j.kind === "repo_update") && j.status === "running");
  const allReady = repos.length > 0 && repos.every(r => r.deployed);
  const wrap = el("div", { class: "first-boot-page" });

  wrap.append(el("button", {
    class: "first-boot-close",
    type: "button",
    title: "Close setup view",
    "aria-label": "Close setup view",
    onclick: leaveSetup,
  }, ico("x")));

  wrap.append(el("div", { class: "first-boot-mark" }, ico(allReady ? "check" : "download")));
  wrap.append(el("h1", {}, allReady ? "Your workspace is ready" :
    active ? "Setting up SCM Workbench" : "Setup needs your attention"));
  wrap.append(el("p", { class: "first-boot-lead" }, allReady
    ? "Both managed repositories are installed. You can start fetching card art and building PDFs."
    : active
      ? "We’re downloading the tools and game definitions the Workbench needs. You can stay here and watch, or explore the app while setup continues in the background."
      : "The initial download stopped before every repository was ready. You can continue into the app and retry from Settings → Managed repo copies."));

  wrap.append(el("div", { class: "card first-boot-progress" },
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("refresh")),
      el("div", { class: "grow" },
        el("h2", {}, "Managed repositories"),
        el("p", {}, active ? "Download and verification progress updates automatically." :
          allReady ? "Everything finished successfully." : "One or more downloads did not finish."))),
    el("div", { class: "repoprog", id: "repoprog", "data-prep-all": "true" })));

  const actions = el("div", { class: "first-boot-actions" });
  if (!allReady && !active) {
    const retry = el("button", { class: "btn primary", type: "button" }, ico("refresh"), "Retry setup");
    retry.onclick = () => retrySetup(retry);
    actions.append(retry,
      el("button", { class: "btn", type: "button", onclick: leaveSetup }, "Continue to the app", ico("arrow")),
      el("span", { class: "small faint" }, "Only repositories that are not ready will be downloaded again."));
  } else {
    actions.append(el("button", { class: "btn primary", type: "button", onclick: leaveSetup },
      allReady ? "Start using the Workbench" : "Explore while setup continues", ico("arrow")));
    if (active) actions.append(el("span", { class: "small faint" },
      "Pages unlock automatically as each repository becomes ready."));
  }
  wrap.append(actions);

  // go() invokes this only after the detached page has entered the document,
  // allowing prep.js to find #repoprog and attach its live row handles here.
  wrap.__patch = updatePrepRows;
  return wrap;
};
