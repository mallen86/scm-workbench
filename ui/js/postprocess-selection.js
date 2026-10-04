/* Compact, metadata-only image picker. No image bytes or arbitrary native calls. */
import { el } from "./core.js";
import { listFiles } from "./artifacts.js";

const ROLES = [["front", "Front"], ["double_sided", "Double-sided"], ["back", "Back"]];
export const SELECTION_PAGE_SIZE = 12;
export function selectionKey(args = {}) {
  return JSON.stringify([args.scope || "both", args.scope === "selected" ? args.selected_images || [] : []]);
}
export function selectionPage(items, query, selectedOnly, selected, page) {
  const needle = query.trim().toLocaleLowerCase();
  const matches = items.filter(item => (!selectedOnly || selected.has(item.id)) &&
    (!needle || item.name.toLocaleLowerCase().includes(needle)));
  const pages = Math.max(1, Math.ceil(matches.length / SELECTION_PAGE_SIZE));
  const current = Math.min(Math.max(0, page), pages - 1);
  return { matches, pages, current, rows: matches.slice(current * SELECTION_PAGE_SIZE, (current + 1) * SELECTION_PAGE_SIZE) };
}

export function createImageSelection({ args, changed, inventory = listFiles }) {
  let items = [], loaded = false, loading = false, disposed = false, page = 0, selectedOnly = false;
  let error = "", generation = 0;
  const inputs = new Map();
  const chosen = () => new Set(Array.isArray(args().selected_images) ? args().selected_images : []);
  const root = el("section", { class: "pp-selection", hidden: true, "aria-label": "Choose cards to process" });
  const search = el("input", { class: "input", type: "search", placeholder: "Search cards…", "aria-label": "Search cards", maxlength: 255,
    oninput: () => { page = 0; paint(); } });
  const count = el("strong", { class: "small", "aria-live": "polite" });
  const message = el("p", { class: "small faint", "aria-live": "polite" });
  const rows = el("div", { class: "pp-selection-grid" });
  const pages = el("span", { class: "small faint", "aria-live": "polite" });
  const previous = el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { page--; paint(); } }, "Previous");
  const next = el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => { page++; paint(); } }, "Next");
  const commit = selected => { args().selected_images = [...selected].sort(); changed(); paint(); };
  const selectMatches = el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => {
    const selected = chosen();
    for (const item of selectionPage(items, search.value, selectedOnly, selected, page).matches) selected.add(item.id);
    if (selected.size > 1024) { error = "Choose up to 1024 images at a time."; paint(); return; }
    commit(selected);
  } }, "Select all matches");
  const clear = el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => commit(new Set()) }, "Clear selection");
  const only = el("input", { type: "checkbox", onchange: () => { selectedOnly = only.checked; page = 0; paint(); } });
  const refresh = el("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => load() }, "Refresh");
  root.append(el("div", { class: "pp-selection-tools" }, search, count),
    el("div", { class: "pp-selection-tools" }, selectMatches, clear,
      el("label", { class: "pp-selection-filter" }, only, "Selected only"), refresh), message, rows,
    el("div", { class: "pp-selection-pages" }, previous, pages, next));

  function paint() {
    const selected = chosen();
    const view = selectionPage(items, search.value || "", selectedOnly, selected, page);
    page = view.current;
    count.textContent = `${selected.size} selected`;
    const missing = loaded ? [...selected].filter(id => !items.some(item => item.id === id)).length : 0;
    message.textContent = loading ? "Loading cards…" : error || (missing
      ? `${missing} selected image${missing === 1 ? " is" : "s are"} no longer available. Clear the selection and choose again.`
      : !items.length ? "No card images found." : !view.matches.length ? "No matching cards." : "Only checked images will be processed.");
    selectMatches.disabled = loading || !loaded || !view.matches.length;
    clear.disabled = !selected.size;
    refresh.disabled = loading;
    inputs.clear();
    rows.replaceChildren(...view.rows.map(item => {
      const checkbox = el("input", { type: "checkbox", checked: selected.has(item.id), "aria-label": `${item.name} (${item.label})`,
        onchange: () => {
          const values = chosen();
          if (checkbox.checked && values.size >= 1024 && !values.has(item.id)) {
            checkbox.checked = false; message.textContent = "Choose up to 1024 images at a time."; return;
          }
          if (checkbox.checked) values.add(item.id); else values.delete(item.id);
          commit(values);
          (inputs.get(item.id) || search).focus?.();
        } });
      inputs.set(item.id, checkbox);
      return el("label", { class: "pp-selection-row", title: item.name }, checkbox,
        el("span", { class: "pp-selection-name" }, item.name.replace(/\.[^.]+$/, "")),
        el("span", { class: "small faint pp-selection-role" }, item.label));
    }));
    pages.textContent = `${view.matches.length} cards · Page ${page + 1} of ${view.pages}`;
    previous.disabled = page === 0; next.disabled = page + 1 >= view.pages;
  }
  async function load() {
    const ticket = ++generation;
    loading = true; error = ""; items = []; loaded = false; paint();
    try {
      const listings = await Promise.all(ROLES.map(([role]) => inventory(`game/${role}`, true)));
      if (disposed || ticket !== generation) return;
      if (listings.some(list => list.truncated)) throw new Error("Too many images to show safely. Reduce the folder contents and refresh.");
      const found = [];
      for (let i = 0; i < listings.length; i++) {
        const [role, label] = ROLES[i];
        for (const item of listings[i].items || []) {
          if (item.dir || typeof item.name !== "string" || /[/\\:\p{C}]/u.test(item.name) ||
              new TextEncoder().encode(item.name).length > 255 || !/\.(png|jpe?g|gif|webp|bmp)$/i.test(item.name)) continue;
          found.push({ id: `game/${role}/${item.name}`, name: item.name, label });
        }
      }
      if (found.length > 3072) throw new Error("Too many images to show safely.");
      items = found.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }) || a.id.localeCompare(b.id));
      loaded = true;
    } catch (cause) { if (!disposed && ticket === generation) error = cause?.message || "Could not load cards. Try Refresh."; }
    finally { if (!disposed && ticket === generation) { loading = false; paint(); if (loaded) changed(); } }
  }
  return { root, sync() {
    root.hidden = args().scope !== "selected";
    if (!root.hidden && !loaded && !loading && !error) void load();
    else paint();
  }, dispose() { disposed = true; generation++; } };
}
