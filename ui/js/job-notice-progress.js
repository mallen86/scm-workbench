/* Describe only progress the worker actually reports. Image counts are
   validated against the staged batch; library installs expose stages, not a
   made-up download percentage. */

const providerName = {
  CPUExecutionProvider: "CPU", CUDAExecutionProvider: "CUDA",
  CoreMLExecutionProvider: "CoreML", DmlExecutionProvider: "DirectML",
};

export function jobNoticeProgress(job, status = job?.status) {
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
        ? `${activity.phase === "initializing" ? "Initializing" : "Processing"} ${activity.name.slice(0, 64)} on ${providerName[activity.provider]}${activity.tiles ? `, tile ${activity.tile} / ${activity.tiles}` : ""}`
        : "Preparing next image…";
      return {
        text: `${warning}${current} / ${total} images processed (${Math.floor(current * 100 / total)}%). ${detail}`,
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
    if (status === "ok") return { text: (fallback ? fallback + "used. " : "") + (job.postprocess_outcome === "unchanged"
      ? "Processing complete; originals unchanged."
      : job.postprocess_outcome === "committed" ? "Images processed and saved." : "Processing complete."), fraction: null };
    if (job.postprocess_outcome === "needs_attention") return {
      text: "Rollback could not be verified; inspect image folders.", fraction: null,
    };
    if (status === "killed") return { text: (fallback ? fallback + "attempted. " : "") + (job.postprocess_outcome === "unchanged"
      ? "Stopped; originals unchanged." : "Processing stopped."), fraction: null };
    if (status === "fail") return { text: (fallback ? fallback + "attempted. " : "") + (job.postprocess_outcome === "unchanged"
      ? "Processing failed; originals unchanged." : "Processing failed; check Job history."), fraction: null };
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
