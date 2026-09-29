export const RECENT_FETCH_LIMIT = 6;
export const RECENT_GAME_LIMIT = RECENT_FETCH_LIMIT - 1;


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


export function recentFetchLayout(jobs, availableSlugs, customArtUsed = false) {
  const all = Array.isArray(availableSlugs) ? [...availableSlugs] : [];
  const customPinned = customArtUsed === true && all.includes("__custom_art");
  const recent = recentFetchPlugins(jobs, all.filter(slug => slug !== "__custom_art"));
  const recentPlugins = customPinned ? [...recent.slice(0, RECENT_GAME_LIMIT), "__custom_art"] : recent;
  return {
    all,
    recent: recentPlugins,
    hasRecent: recentPlugins.length > 0,
    allOpen: recentPlugins.length === 0,
  };
}
