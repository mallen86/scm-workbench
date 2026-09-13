/* Pure view state for the Create PDF card-back control. Keeping path and
   inventory decisions outside the DOM makes the Simple/Advanced behavior
   executable as a small frontend contract. */

export const DEFAULT_BACK_DIRECTORY = "game/back";


export function backDirectory(raw) {
  const value = String(raw ?? "").trim();
  return value || DEFAULT_BACK_DIRECTORY;
}


export function isDefaultBackDirectory(raw) {
  let value = backDirectory(raw);
  while (value.startsWith("./")) value = value.slice(2);
  value = value.replace(/\/+$/, "");
  return value === DEFAULT_BACK_DIRECTORY;
}


export function backImageState({
  dir,
  items = [],
  found,
  exists = true,
  truncated = false,
  unavailable = false,
  onlyFronts = false,
} = {}) {
  const directory = backDirectory(dir);
  const defaultDirectory = isDefaultBackDirectory(directory);
  const safeItems = Array.isArray(items) ? items : [];
  const count = Number.isSafeInteger(found) && found >= 0 ? found : safeItems.length;
  let status = "No recognized image.";
  let tone = "";

  if (onlyFronts) {
    status = "Not used for front pages only.";
    tone = "muted";
  } else if (unavailable) {
    status = "Could not check this folder.";
    tone = "warn";
  } else if (!exists) {
    status = "Folder not found.";
    tone = "warn";
  } else if (truncated) {
    status = "Could not safely check every file.";
    tone = "warn";
  } else if (count === 1) {
    status = safeItems[0]?.name || "1 recognized image.";
  } else if (count > 1) {
    status = `${count} recognized images. Keep exactly one.`;
    tone = "warn";
  }

  return {
    directory,
    defaultDirectory,
    count,
    status,
    tone,
    onlyFronts,
    canImport: defaultDirectory && !onlyFronts,
    canReveal: !onlyFronts && exists && !unavailable,
  };
}
