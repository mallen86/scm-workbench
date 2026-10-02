/* Describe only progress the worker actually reports. Image counts are
   validated against the staged batch; library installs expose stages, not a
   made-up download percentage. */

const providerName = {
  CPUExecutionProvider: "CPU", CUDAExecutionProvider: "CUDA",
  CoreMLExecutionProvider: "CoreML", DmlExecutionProvider: "DirectML",
};

// Optional public failure details are plain text, never log HTML. Fail closed
// on malformed or oversized values, including multibyte strings.
export function postprocessFailureReason(job, status = job?.status) {
  const reason = job?.postprocess_error;
  if (job?.kind !== "postprocess_images" || status !== "fail" ||
      typeof reason !== "string" || reason.length > 2048 ||
      new TextEncoder().encode(reason).length > 2048 ||
      /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(reason)) return "";
  return reason.trim();
}

// Skips are independent of the batch's commit/rollback outcome. Treat even
// persisted/native summaries as untrusted, and render their contents as text.
export function postprocessSkipSummary(job) {
  const summary = job?.postprocess_skips;
  const exactKeys = (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  const bounded = (text, bytes) => typeof text === "string" && text.trim() &&
    !/\p{C}/u.test(text) && new TextEncoder().encode(text).length <= bytes;
  if (job?.kind !== "postprocess_images" || !exactKeys(summary, ["count", "total", "reasons"])) return null;
  const { count, total, reasons } = summary;
  if (!Number.isSafeInteger(count) || !Number.isSafeInteger(total) || count < 1 || count > total || total > 1024 ||
      !Array.isArray(reasons) || reasons.length < 1 || reasons.length > Math.min(count, 8) ||
      reasons.some(item => !exactKeys(item, ["name", "role", "reason"]) ||
        !["front", "double_sided", "back"].includes(item.role) || !bounded(item.name, 255) ||
        [".", ".."].includes(item.name) || /[/\\\\]/.test(item.name) || !bounded(item.reason, 256)) ||
      new Set(reasons.map(item => `${item.role}/${item.name}`)).size !== reasons.length ||
      new TextEncoder().encode(JSON.stringify(summary)).length > 4096) return null;
  const text = `${count} image${count === 1 ? "" : "s"} left unchanged.`;
  const details = reasons.map(item => {
    const reason = ["embedded resolution is already at or above 1200 ppi",
      "pixels already meet or exceed the standard mtg fallback fit target"].includes(item.reason.toLowerCase())
      ? "Already large enough for sharp printing." : item.reason;
    return `game/${item.role}/${item.name}: ${reason}`;
  });
  const omitted = count - reasons.length;
  const tail = omitted ? `${omitted} more skip reason${omitted === 1 ? "" : "s"} in the job log.` : "";
  return { count, total, text, details, tail, compactText: text };
}

// Pass the DOM builder in so validation/progress remain a pure module.
export function postprocessSkipDetails(job, el) {
  const skips = postprocessSkipSummary(job);
  if (!skips) return null;
  return el("details", { class: "small pp-skips", onclick: event => event.stopPropagation() },
    el("summary", {}, "Details"),
    ...skips.details.map(text => el("div", {}, text)),
    skips.tail ? el("div", {}, skips.tail) : null);
}

export function jobNoticeProgress(job, status = job?.status) {
  const skips = postprocessSkipSummary(job);
  const skipDetail = skips ? ` ${skips.compactText}` : "";
  if (status === "running" && job?.kind === "postprocess_images") {
    const current = job.progress?.current, total = job.progress?.total;
    if (Number.isSafeInteger(current) && Number.isSafeInteger(total) &&
        total > 0 && total <= 1024 && current >= 0 && current <= total) {
      const activity = job.progress?.activity;
      const validActivity = activity && activity.index === current + 1 &&
        activity.total === total && typeof activity.name === "string" && activity.name.length <= 255 &&
        Object.hasOwn(providerName, activity.provider) &&
        ["initializing", "fallback", "tile"].includes(activity.phase) &&
        Number.isSafeInteger(activity.tile) && Number.isSafeInteger(activity.tiles) &&
        activity.tile >= 0 && activity.tile <= activity.tiles && activity.tiles <= 1000000;
      const warning = job.postprocess_cpu_reason === "missing_cudnn" && ["cuda", "cuda13"].includes(job.postprocess_cpu_warning)
        ? `CPU fallback: cuDNN 9 (libcudnn.so) is missing; using CPU instead of CUDA ${job.postprocess_cpu_warning === "cuda13" ? "13" : "12"}. AI processing may be slow. `
        : job.postprocess_cpu_warning === "cuda" || job.postprocess_cpu_warning === "cuda13"
        ? `CPU fallback: NVIDIA CUDA needs CUDA ${job.postprocess_cpu_warning === "cuda13" ? "13" : "12"}.x and cuDNN 9. AI processing may be slow. `
        : job.postprocess_cpu_warning === "gpu" ? "GPU unavailable; using CPU. AI processing may be slow. " : "";
      const detail = current === total ? "Validating results…" : validActivity
        ? `${activity.phase === "initializing" ? "Initializing" : "Processing"} image on ${providerName[activity.provider]}${activity.tiles ? `, tile ${activity.tile} / ${activity.tiles}` : ""}`
        : "Preparing next image…";
      return {
        text: `${warning}${current} / ${total} images ${skips ? "checked" : "processed"} (${Math.floor(current * 100 / total)}%). ${detail}${skipDetail}`,
        fraction: current / total, warning: warning.trim(),
      };
    }
    return { text: "Preparing image set…", fraction: null };
  }
  if (status === "running" && job?.kind === "postprocess_dependencies") {
    const stage = job.progress?.label;
    return { text: typeof stage === "string" && stage.length <= 120 && stage.trim()
      ? stage : "Preparing installer…", fraction: null };
  }
  if (job?.kind === "postprocess_images") {
    const fallback = job.postprocess_cpu_reason === "missing_cudnn" && ["cuda", "cuda13"].includes(job.postprocess_cpu_warning)
      ? "cuDNN 9 (libcudnn.so) missing; CPU fallback " : job.postprocess_cpu_warning ? "CPU fallback " : "";
    const reason = postprocessFailureReason(job, status);
    const failureDetail = reason ? ` ${reason}` : "";
    if (job.postprocess_outcome === "needs_attention") return {
      text: "Rollback could not be verified; inspect image folders." + failureDetail + skipDetail, fraction: null,
    };
    if (status === "ok") {
      const outcome = job.postprocess_outcome;
      const counted = skips && (outcome === "unchanged" ||
        (outcome === "committed" && skips.count < skips.total));
      const result = outcome === "unchanged"
        ? (skips ? `Finished: ${skips.total} image${skips.total === 1 ? "" : "s"} left unchanged.` : "Processing complete; originals unchanged.")
        : outcome === "committed" ? (counted
          ? `Finished: ${skips.total - skips.count} image${skips.total - skips.count === 1 ? "" : "s"} resized, ${skips.count} left unchanged.`
          : skips ? "Processing complete." : "Images processed and saved.") : "Processing complete.";
      return { text: (fallback ? fallback + "used. " : "") + result +
        (counted ? "" : skipDetail), fraction: null };
    }
    if (status === "killed") return { text: (fallback ? fallback + "attempted. " : "") + (job.postprocess_outcome === "unchanged"
      ? "Stopped; originals unchanged." : "Processing stopped.") + skipDetail, fraction: null };
    if (status === "fail") return { text: (fallback ? fallback + "attempted. " : "") + (job.postprocess_outcome === "unchanged"
      ? "Processing failed; originals unchanged." : "Processing failed; check Job history.") + failureDetail + skipDetail, fraction: null };
  }
  if (job?.kind === "postprocess_dependencies") {
    if (status === "ok") return { text: "Libraries installed and ready.", fraction: null };
    if (status === "killed") return { text: "Installation stopped.", fraction: null };
    if (status === "fail") return { text: "Installation failed; see Image post-processing.", fraction: null };
  }
  if (status === "running") return { text: "This job is still working.", fraction: null };
  if (status === "ok") return { text: "Done.", fraction: null };
  if (status === "killed") return { text: "Stopped.", fraction: null };
  return { text: "The job did not finish.", fraction: null };
}
