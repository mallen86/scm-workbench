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

If the window comes up and stays on the embedded splash, first check
**Gatekeeper**. If the app was just updated or freshly downloaded, *System
Settings → Privacy & Security* may show "SCM Workbench from an unknown
developer was prevented from opening" — click **Open Anyway**, then launch it
again. This gate can return after an in-app update because the updater applies
a fresh ad-hoc signature.

The WebView now loads embedded assets and talks to its worker through native
IPC, so **Local Network permission is not required**. If the worker cannot
start, the splash changes to an in-place diagnostic page with **Try again** and
**Copy report** controls. Use that report and the path it displays rather than
re-downloading: a quarantined copy that passed the shipped signature check is
not repaired by downloading the same bytes again. If macOS leaves the newly
signed WebView in a stale state, quit every SCM Workbench instance and reopen
it; reboot only if that does not clear the system state.

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
