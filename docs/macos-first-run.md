# First run on macOS — and when the window gets stuck

## The one-click it needs (only once, per machine, per version)

The app is **ad-hoc signed, not notarized** (a Developer ID + notarization is
the only way to make this step disappear, and it is not available yet).
So a freshly downloaded copy is gated by macOS:

1. Open **scm-workbench-macos.dmg**, drag **SCM Workbench** onto the
   **Applications** icon, eject the disk image, and open the installed app.
2. If macOS says *"couldn't be opened / is from an unknown developer"*, go to
   **System Settings → Privacy & Security**, scroll to the bottom, and click
   **"Open Anyway"** for SCM Workbench.
3. Open the app once more. It works from then on — the allow is remembered by
   the machine, not per copy.

The in-app updater now prefers the signed `scm-workbench-macos.dmg`: it
mounts it read-only, copies the validated app out, detaches it, and then hands
the candidate to the native updater. Releases also retain
`scm-workbench-macos.zip` as a legacy bridge for older checked metadata.

Two things that make this step feel broken:

- **Don't run the app from the mounted installer or `~/Downloads`.** macOS can
  *translocate* a downloaded app and run a temporary copy under
  `/private/var/folders/.../AppTranslocation/...`. Always use the copy you
  dragged into Applications, then eject the disk image.
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

The WebView loads embedded assets and talks to its worker through native
IPC. The packaged worker binds no TCP listener, so **Local Network permission is not required**. If the worker cannot
start, the splash changes to an in-place diagnostic page with **Try again** and
**Copy report** controls. Use that report and the path it displays rather than
re-downloading: a quarantined copy that passed the shipped signature check is
not repaired by downloading the same bytes again. If macOS leaves the newly
signed WebView in a stale state, quit every SCM Workbench instance and reopen
it; reboot only if that does not clear the system state.

## "The worker did not become ready"

The shell uses a bounded native JSON-lines handshake and shows the worker log
in the embedded failure page if the child dies or times out. It does not probe,
claim, or reclaim a TCP port. Quit any other SCM Workbench windows (⌘Q), then
use **Try again**; if the problem persists, the report's data path is the
place to inspect.
