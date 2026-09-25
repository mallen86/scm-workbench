/* Present the latest optional-install attempt independently of the transient
   sidebar notice. The result remains meaningful after a page rebuild/reload. */

export function latestInstallJob(jobs, processorId) {
  if (!processorId) return null;
  return (jobs || [])
    .filter(job => job.kind === "postprocess_dependencies" && job.args?.processor_id === processorId)
    .sort((a, b) => (b.ts || 0) - (a.ts || 0))[0] || null;
}

export function optionalInstallStatus(processor, job, failure, startError, activity = null) {
  if (!processor?.optional_model) return { label: "", tone: "" };
  if (startError?.processorId === processor.id &&
      (!job || startError.at >= (Number(job.ts) || 0) * 1000)) {
    return { label: `Installation did not start. ${startError.message}`, tone: "fail" };
  }
  if (job?.status === "running") {
    const minutes = Math.max(0, Math.floor((Date.now() / 1000 - Number(job.ts || 0)) / 60));
    const elapsed = job.ts && minutes > 0 ? ` ${minutes} min elapsed.` : "";
    const step = activity?.id === job.id && activity.step ? ` Current step: ${activity.step}` : " Waiting for installer output…";
    return { label: `Installing model and libraries… Job ${job.id} is running.${elapsed}${step}`, tone: "running" };
  }
  if (job?.status === "fail") return {
    label: `Installation failed (job ${job.id}). ${failure?.id === job.id ? failure.message : "Loading the job's failure details…"}`,
    tone: "fail",
  };
  if (job?.status === "killed") return { label: "Installation cancelled. Images were not changed.", tone: "killed" };
  if (processor.ready_to_run === true) return { label: "Installed and ready to run offline.", tone: "ok" };
  if (job?.status === "ok") return { label: "The previous installation is no longer ready. Install again for this Python runtime.", tone: "fail" };
  return { label: "Not installed. The Simple Upscaler remains available without a download.", tone: "missing" };
}
