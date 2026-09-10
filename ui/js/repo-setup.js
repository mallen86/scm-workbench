/* repo-setup: the "Connect your repos" card, shared by every page whose work
   needs a repository. It appears only when the user has to act: the repo is
   not found AND the app is not auto-fetching its own managed copy. While a
   managed clone/update is in flight the prep card (or the sidebar strip) is
   the single source of truth, and a custom folder can always be pointed in
   from Settings. */

import { S, el, ico, toast } from "./core.js";
import { refreshInfo } from "./info.js";
import { go } from "./nav.js";
import { prepActive } from "./prep.js";
import { setSettings } from "./settings-transport.js";


export function connectCardNeeded() {
  if (!S.info) return true;
  if (S.info.scm.found) return false;
  // hidden only while the app is actively preparing (bootstrap flag set) —
  // a failed/stopped prep must still show the card so a folder can be pointed in
  if (S.info.server.is_packaged && S.info.server.active) return false;
  return true;
}


export function repoSetupCard() {
  const wrap = el("div", { class: "card" });
  wrap.append(
    el("div", { class: "card-head" },
      el("div", { class: "card-ico" }, ico("folder")),
      el("div", { class: "grow" },
        el("h2", {}, "Connect your repos"),
        el("p", {}, S.info.server.is_packaged && prepActive()
          ? "Managed copies of both repos are downloading. Pages connect as each repo is ready. A path you enter takes priority."
          : "Workbench could not find silhouette-card-maker next to this project. Enter repo paths below."),
      ),
    ),
  );
  const row = el("div", { class: "frow" });
  const scmInp = el("input", { class: "input mono", placeholder: "/path/to/silhouette-card-maker" });
  const exInp = el("input", { class: "input mono", placeholder: "/path/to/scm-extras (optional)" });
  row.append(
    el("div", { class: "field w-half" }, el("label", {}, "silhouette-card-maker"), scmInp),
    el("div", { class: "field w-half" }, el("label", {}, "scm-extras (optional)"), exInp),
  );
  wrap.append(row);
  wrap.append(el("div", { style: "margin-top:14px; display:flex; gap:10px; align-items:center" },
    el("button", { class: "btn primary", onclick: async () => {
      const r = await setSettings({ scm_dir: scmInp.value.trim(), extras_dir: exInp.value.trim() });
      await refreshInfo();
      toast(S.info.scm.found ? "ok" : "err", S.info.scm.found ? "Connected. Reloaded this page." : "Still not found. Check the path and try again.");
      go(S.page || "history", null, { push: false });
    } }, ico("check"), "Save & reconnect"),
    el("span", { class: "faint small" }, "Paths are saved in this project's data/settings.json."),
  ));
  return wrap;
}
