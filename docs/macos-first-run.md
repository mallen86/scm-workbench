# First run on macOS — and when the window gets stuck

## The one-click it needs (only once, per machine, per version)

The app is **ad-hoc signed, not notarized** (a Developer ID + notarization is
the only way to make this step disappear, and it is not available yet).
So a freshly downloaded copy is gated by macOS:

1. Double-click **SCM Workbench.app** (or the zip, then the app inside it).
2. If macOS says *"couldn't be opened / is from an unknown developer"*, go to
   **System Settings → Privacy & Security**, scroll to the bottom, and click
   **"Open Anyway"** for SCM Workbench.
3. Open the app once more. It works from then on — the allow is remembered by
   the machine, not per copy.

Two things that make this step feel broken:

- **Don't run it from `~/Downloads`.** An app downloaded over the web and
  launched straight from Downloads is *translocated* by macOS: it runs from a
  temporary copy under `/private/var/folders/.../AppTranslocation/...` instead
  of from your folder, and the per-copy state (including the local-network
  allow below) is keyed to that temporary identity. Every fresh download is a
  fresh identity. The clean flow is: **move the app (drag it, in Finder) to
  Applications**, then run it from there.
- The allow in step 2 is per *signature*. Each release is re-signed with a new
  ad-hoc seal, so **each new version gets its one "Open Anyway"**. This is
  true of the in-app updater too: it swaps the bundle in place, the new seal
  makes the relaunched copy an unknown developer again, and the next launch
  sits blocked behind *System Settings → Privacy & Security → Open Anyway*
  until you click it — **a window that opens blank right after an update is
  that gate, not a broken update**. Click it once; the build is remembered.

## "The window opens but it never loads"

If the window comes up and just sits there (splash, or a blank page), there
are two gates, in this order:

1. **Gatekeeper** — if the app was *just updated or freshly downloaded*, the
   launch is blocked outright: *System Settings → Privacy & Security* shows
   "SCM Workbench from an unknown developer was prevented from opening" —
   click **Open Anyway** there, then launch it again. (This is the one users
   hit after in-app updates; see the update section below.)
2. **Local Network** permission, which current macOS asks for separately:

- **System Settings → Privacy & Security → Local Network** — make sure
  *SCM Workbench* is switched **on**.
- If it isn't in the list at all, launch the app once from **Applications**
  (not Downloads, not a terminal) and the prompt appears; answer **Allow**.
  The window needs plain-HTTP access to its own worker on `127.0.0.1:8038`;
  without the grant the webview is denied and no app version can load.
- **The one-time gates return after an in-app update, too.** The updater
  re-signs the bundle in place with a fresh ad-hoc seal, so macOS treats the
  relaunched copy as a brand-new, never-seen app: expect the same
  **"Open Anyway"** entry under *Privacy & Security* as on first install
  (click it once; it is remembered for this build), and possibly the local-
  network question as well (allow it — the window loads through either).
  The updater's closing message names both, so a blocked relaunch has an
  answer instead of a mystery.

What it is *not*: a broken download. A quarantined copy still passes the
signature check (the app verifies its own seal at build time, and a copy that
failed that check never ships), so a stuck window is a state of the webview
on that machine after a fresh signature, not damaged bytes — re-downloading
the same version won't change it. On current macOS the reliable clear is a
reboot (or the Open Anyway / Local Network steps above, then a re-open); the
window's own splash message now tells you which one to try first.

## "The worker died at start-up, and its port (8038) is already taken…"

That page means a **leftover** from a previous instance (a window that was
hard-killed, or a translocated copy that was cleaned up) is still holding the
port, and this launch correctly declined to kill a process it didn't spawn.
Fixes, in order:

1. Quit any SCM Workbench windows that are still open (⌘Q — not just
   closing the window if the app is still in the Dock).
2. If it persists: delete the app's data area and open it fresh. The path is
   on the page; it is typically
   `~/Library/Application Support/scm-workbench`.
3. If *that* persists, the stale listener can be found with:
   `lsof -nP -i :8038 -sTCP:LISTEN` — the pid shown is the holder.
