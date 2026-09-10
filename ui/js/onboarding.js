/* onboarding: the one-time welcome card. It belongs to the first-run setup
   screen and appears there once the managed repos are ready, so the staged
   workflow it describes is the next thing the user sees after setup. The
   dismissed flag lives in the settings file, so it never comes back. */

import { S, el } from "./core.js";
import { bootPage } from "./nav.js";
import { setSettings } from "./settings-transport.js";


export function onboardCard(onDone) {
  const steps = [
    ["1", "Calibrate & offset", "Print a calibration sheet, measure the drift, and save the X/Y/angle correction. Create PDF applies it when enabled."],
    ["2", "Fetch card art", "Choose a game and decklist. Card art goes to game/front."],
    ["3", "Create the PDF", "Lay out cards on your paper with registration marks. Print both sides when needed."],
    ["4", "Cut with a template", "Open the matching .studio3 template in Silhouette Studio and cut your cards."],
  ];
  return el("div", { class: "onboard" },
    el("h2", {}, "Welcome to the Workbench"),
    el("p", { class: "muted", style: "margin-top:6px; font-size:13px; max-width:720px; line-height:1.55" },
      "This console runs scripts from silhouette-card-maker and scm-extras, so no terminal is needed. The workflow has four stages:"),
    el("div", { class: "steps" },
      steps.map(([n, t, d]) => el("div", { class: "step" },
        el("div", { class: "n" }, n), el("div", { class: "t" }, t), el("div", { class: "d" }, d),
      )),
    ),
    el("div", { style: "margin-top:16px" },
      el("button", { class: "btn primary", onclick: async () => {
        await setSettings({ onboarded: true });
        S.info.settings.onboarded = true;
        // The card only exists on the setup screen, so dismissing it means
        // leaving that screen; the caller may pick a different landing page.
        if (typeof onDone === "function") onDone();
        else bootPage();
      } }, "Got it. Show me around"),
    ),
  );
}
