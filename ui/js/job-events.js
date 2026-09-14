export const JOBS_UPDATED_EVENT = "wb:jobs-updated";


export function publishJobsUpdated() {
  if (typeof globalThis.document?.dispatchEvent !== "function" ||
      typeof globalThis.CustomEvent !== "function") return;
  globalThis.document.dispatchEvent(new globalThis.CustomEvent(JOBS_UPDATED_EVENT));
}
