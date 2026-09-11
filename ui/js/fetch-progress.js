/* fetch-progress — the two-stage progress model for a prefetching fetch
   plugin (MTG over an MPCFill XML decklist).

   That plugin runs two separate passes and announces them differently:

     Prefetching 88 images with 8 workers...   <- stage 1 total
       Fetched 10/88 images                    <- stage 1 counter
       Fetched 88/88 images
     Prefetch complete.                        <- stage 1 is done
     Slot 1: Akroma's Will [LCC] {125}         <- stage 2 counter
     Slot 2: Anguished Unmaking (Full)
     ...

   Stage 2 announces no total, so it is measured against stage 1's number:
   a deck can have more slots than unique images (a card played four times is
   one image and four slots). Past that number the counter keeps moving and
   the bar holds at full, rather than freezing the numbers or inventing a
   denominator the plugin never printed.

   Kept free of DOM and imports so the stage machine can be tested directly. */

/** How long stage 1's full bar stays on screen before stage 2 restarts at 0.
    Without a beat here the two lines arrive together and the first 100% is
    never visible. */
export const RENAME_HOLD_MS = 450;

export function createFetchProgress() {
  let total = 0;      // stage 1's announced total
  let deckTotal = 0;  // the decklist's slot count, when it declares one
  let prefetched = 0; // stage 1 counter
  let slots = 0;      // stage 2 counter
  let seen = false;   // a prefetch stage exists in this run
  let renaming = false;

  const percent = (done, whole) => whole > 0
    ? Math.min(100, Math.round(done / whole * 100)) : 0;

  return {
    /** True once a prefetch stage is known, so the caller stops reading "n/m"
         as its own single-stage progress. */
    get active() { return seen; },

    /** Consume one output line. Returns what the caller should do, or null for
        a line that is not part of a prefetch run. */
    line(text) {
      const start = /Prefetching\s+(\d+)\s+images/i.exec(text);
      if (start) {
        seen = true;
        total = +start[1];
        prefetched = 0;
        return { stage: 1, beginRename: false };
      }
      if (/Prefetch complete\.?/i.test(text)) {
        seen = true;
        prefetched = total || prefetched;
        return { stage: 1, beginRename: true };
      }
      const slot = /^\s*Slot\s+(\d+)\s*:/i.exec(text);
      if (slot) {
        if (!seen) return null;
        slots = Math.max(slots, +slot[1]);
        return { stage: renaming ? 2 : 1, beginRename: false };
      }
      const fraction = /(\d+)\s*\/\s*(\d+)/.exec(text);
      if (fraction) {
        if (!seen) return null;
        prefetched = +fraction[1];
        total = +fraction[2];
        return { stage: renaming ? 2 : 1, beginRename: false };
      }
      return null;
    },

    /** The decklist's own slot count, which is what stage 2 walks. Unknown
        (0) falls back to stage 1's number, the images actually fetched. */
    setDeckTotal(count) {
      deckTotal = Number(count) > 0 ? Number(count) : 0;
    },

    /** Switch to stage 2, after the caller has shown stage 1 complete. */
    beginRename() {
      renaming = true;
      return { stage: 2, beginRename: false };
    },

    /** The numbers line for the stage in progress, or null before a total. */
    view() {
      if (!seen || total <= 0) return null;
      // Stage 2 is measured against the decklist's own slot count when it has
      // one: a card played four times is one image and four slots, so the
      // prefetch number can be well short of the slots that follow.
      const whole = renaming && deckTotal > 0 ? deckTotal : total;
      const done = renaming ? slots : prefetched;
      const pct = percent(done, whole);
      return {
        stage: renaming ? 2 : 1,
        done,
        total: whole,
        pct,
        // A count past the total means the decklist understated itself; the
        // count still moves, but no ratio may be claimed for it.
        text: renaming && done > whole
          ? done + " slots renamed"
          : `${done}/${whole} (${pct}%)`,
      };
    },
  };
}
