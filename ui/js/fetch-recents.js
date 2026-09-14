export const RECENT_FETCH_LIMIT = 6;


export function recentFetchPlugins(jobs, availableSlugs) {
  const available = new Set(Array.isArray(availableSlugs)
    ? availableSlugs.filter(slug => typeof slug === "string") : []);
  const ordered = (Array.isArray(jobs) ? jobs.slice(0, 1000) : [])
    .map((job, index) => ({ job, index }))
    .sort((left, right) => {
      const leftTime = typeof left.job?.ts === "number" && Number.isFinite(left.job.ts)
        ? left.job.ts : 0;
      const rightTime = typeof right.job?.ts === "number" && Number.isFinite(right.job.ts)
        ? right.job.ts : 0;
      return rightTime - leftTime || left.index - right.index;
    });
  const recent = [];
  const seen = new Set();
  for (const { job } of ordered) {
    const kind = typeof job?.kind === "string" ? job.kind : "";
    if (!kind.startsWith("fetch:")) continue;
    const slug = kind.slice(6);
    if (!available.has(slug) || seen.has(slug)) continue;
    seen.add(slug);
    recent.push(slug);
    if (recent.length === RECENT_FETCH_LIMIT) break;
  }
  return recent;
}


export function recentFetchLayout(jobs, availableSlugs) {
  const all = Array.isArray(availableSlugs) ? [...availableSlugs] : [];
  const recent = recentFetchPlugins(jobs, all);
  return {
    all,
    recent,
    hasRecent: recent.length > 0,
    allOpen: recent.length === 0,
  };
}
