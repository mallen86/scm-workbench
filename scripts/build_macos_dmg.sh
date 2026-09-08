#!/usr/bin/env bash
# Build the user-facing drag-to-Applications disk image around an assembled app.
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <SCM Workbench.app> <background.png> <output.dmg>" >&2
  exit 2
fi

app_input="$1"
background_input="$2"
output_input="$3"
[[ -d "$app_input" ]] || { echo "app bundle not found: $app_input" >&2; exit 2; }
[[ -f "$background_input" ]] || { echo "DMG background not found: $background_input" >&2; exit 2; }

app="$(cd "$(dirname "$app_input")" && pwd -P)/$(basename "$app_input")"
background="$(cd "$(dirname "$background_input")" && pwd -P)/$(basename "$background_input")"
output_dir="$(cd "$(dirname "$output_input")" && pwd -P)"
output="$output_dir/$(basename "$output_input")"
volume_name="SCM Workbench"
work="$(mktemp -d /private/tmp/scm-workbench-dmg.XXXXXX)"
root="$work/root"
mount=""
readwrite="$work/installer-rw.dmg"
attached=""

cleanup() {
  if [[ -n "$attached" ]]; then
    hdiutil detach "$mount" -quiet >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

mkdir -p "$root/.background"
ditto "$app" "$root/SCM Workbench.app"
ln -s /Applications "$root/Applications"
cp "$background" "$root/.background/background.png"
codesign --verify --verbose=2 "$root/SCM Workbench.app"

# A writable intermediate lets Finder persist icon positions, window geometry,
# hidden chrome, and the custom background into the volume's .DS_Store.
hdiutil create -quiet -ov -format UDRW -fs HFS+ -volname "$volume_name" \
  -srcfolder "$root" "$readwrite"
attach_plist="$work/attach.plist"
hdiutil attach -plist -readwrite -noverify -noautoopen -nobrowse \
  -mountrandom /Volumes "$readwrite" > "$attach_plist"
mount="$(python3 - "$attach_plist" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as source:
    document = plistlib.load(source)
mounts = [entry.get("mount-point") for entry in document.get("system-entities", [])
          if entry.get("mount-point")]
if len(mounts) != 1:
    raise SystemExit("DMG intermediate did not expose exactly one mount point")
print(mounts[0])
PY
)"
attached=1
disk_name="$(basename "$mount")"

# Finder can race a freshly attached disk on both local machines and CI.
sleep 5
osascript - "$disk_name" "$mount" <<'APPLESCRIPT'
on run argv
  set diskName to item 1 of argv
  tell application "Finder"
    tell disk (diskName as string)
      open
      set xOrigin to 120
      set yOrigin to 120
      set windowWidth to 660
      set windowHeight to 400
      tell container window
        set current view to icon view
        set toolbar visible to false
        set statusbar visible to false
        set pathbar visible to false
        set the bounds to {xOrigin, yOrigin, xOrigin + windowWidth, yOrigin + windowHeight}
        -- Even when the user shows hidden files, keep support assets off-canvas.
        set position of every item to {windowWidth + 100, 100}
      end tell
      set viewOptions to the icon view options of container window
      tell viewOptions
        set arrangement to not arranged
        set icon size to 112
        set text size to 13
      end tell
      set background picture of viewOptions to file ".background:background.png"
      set position of item "SCM Workbench.app" to {170, 225}
      set position of item "Applications" to {490, 225}
      set the extension hidden of item "SCM Workbench.app" to true
      close
      open
      delay 1
      tell container window
        set the bounds to {xOrigin, yOrigin, xOrigin + windowWidth - 10, yOrigin + windowHeight - 10}
      end tell
      delay 1
      tell container window
        set the bounds to {xOrigin, yOrigin, xOrigin + windowWidth, yOrigin + windowHeight}
      end tell
      delay 3
      close
    end tell
  end tell
end run
APPLESCRIPT

chflags hidden "$mount/.background"
sync
test -f "$mount/.DS_Store"
hdiutil detach "$mount" -quiet
attached=""
rm -f "$output"
hdiutil convert -quiet "$readwrite" -format UDZO -imagekey zlib-level=9 -o "$output"
test -f "$output"
