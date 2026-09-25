/* Describe only progress the worker actually reports. Image counts are
   validated against the staged batch; library installs expose stages, not a
   made-up download percentage. */

export function jobNoticeProgress(job, status = job?.status) {
  if (status === "running" && job?.kind === "postprocess_images") {
    const current = job.progress?.current, total = job.progress?.total;
    if (Number.isSafeInteger(current) && Number.isSafeInteger(total) &&
        total > 0 && total <= 1000000 && current >= 0 && current <= total) {
      const percent = Math.floor(current * 100 / total);
      return {
        text: current === total
          ? `${current} / ${total} images processed; validating results…`
          : `${current} / ${total} images processed (${percent}%)`,
        fraction: current / total,
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
    if (status === "ok") return { text: job.postprocess_outcome === "unchanged"
      ? "Processing complete; originals unchanged."
      : job.postprocess_outcome === "committed" ? "Images processed and saved." : "Processing complete.", fraction: null };
    if (job.postprocess_outcome === "needs_attention") return {
      text: "Rollback could not be verified; inspect image folders.", fraction: null,
    };
    if (status === "killed") return { text: job.postprocess_outcome === "unchanged"
      ? "Stopped; originals unchanged." : "Processing stopped.", fraction: null };
    if (status === "fail") return { text: job.postprocess_outcome === "unchanged"
      ? "Processing failed; originals unchanged." : "Processing failed; check Job history.", fraction: null };
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
