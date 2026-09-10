/* pages/history: the dedicated Job history page, and the app's landing page.
   Available in both interface modes: it lists every job the workbench has run
   (live and persisted) and, when a job has a page, opens that page with the
   exact settings the job ran with. The list itself lives in job-history.js so
   the jobs poll can repaint it in place; go() asks refreshJobs(true) for a
   fresh paint on arrival.

   The two cards a first run needs (connect the repos, the one-time welcome)
   live here because the dashboard that used to own them is gone; both hide
   themselves as soon as they no longer apply. */

import { PAGES, S, el, pageHead } from "../core.js";
import { renderJobHistory } from "../job-history.js";
import { onboardCard } from "../onboarding.js";
import { connectCardNeeded, repoSetupCard } from "../repo-setup.js";


PAGES.history = () => {
  const wrap = el("div", {});
  // No live counts in the head: the sections below carry them, and a number
  // baked at render time would go stale as the poll repaints the list.
  wrap.append(pageHead("Job history",
    "Every job this app has run, newest first. Click a job to open its page with the settings it ran with. Live output stays in the console."));
  if (connectCardNeeded()) wrap.append(repoSetupCard());
  else if (!S.info.settings.onboarded) wrap.append(onboardCard());
  const slot = el("div", { id: "job-history" });
  wrap.append(slot);
  renderJobHistory(slot);   // first paint from the jobs already in hand
  return wrap;
};
