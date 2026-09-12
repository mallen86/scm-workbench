/* Pure lifecycle model for sidebar job notices. Kept separate from the DOM so
   terminal transitions, expiry, dismissal, and bounds have an executable Node
   contract without a browser harness. */

export const JOB_NOTICE_HOLD_MS = 5000;
export const JOB_NOTICE_MAX = 4;
const DISMISSED_MAX = 64;
const SPECIALIZED_KINDS = new Set(["repo_init", "repo_update", "update"]);
const TERMINAL = new Set(["ok", "fail", "killed", "handoff"]);


export function createJobNoticeState() {
  const records = new Map();
  const dismissed = new Map();

  const dismiss = id => {
    records.delete(id);
    dismissed.delete(id);
    dismissed.set(id, true);
    while (dismissed.size > DISMISSED_MAX) dismissed.delete(dismissed.keys().next().value);
  };

  const sync = (jobs, now = Date.now()) => {
    const current = new Map((jobs || []).filter(job => job?.id).map(job => [job.id, job]));

    for (const [id, record] of [...records]) {
      const job = current.get(id);
      if (!job || SPECIALIZED_KINDS.has(job.kind)) {
        records.delete(id);
        continue;
      }
      record.job = job;
      if (record.status === "running" && TERMINAL.has(job.status)) {
        record.status = job.status;
        record.expiresAt = now + JOB_NOTICE_HOLD_MS;
      }
      if (record.expiresAt && record.expiresAt <= now) records.delete(id);
    }

    const running = (jobs || [])
      .filter(job => job?.id && job.status === "running" && !SPECIALIZED_KINDS.has(job.kind))
      .sort((a, b) => (b.ts || 0) - (a.ts || 0));
    for (const job of running) {
      if (records.size >= JOB_NOTICE_MAX) break;
      if (!dismissed.has(job.id) && !records.has(job.id)) {
        records.set(job.id, { job, status: "running", expiresAt: 0 });
      }
    }

    return [...records.values()]
      .sort((a, b) => (b.job.ts || 0) - (a.job.ts || 0))
      .map(record => ({ ...record, job: { ...record.job } }));
  };

  return { sync, dismiss };
}
