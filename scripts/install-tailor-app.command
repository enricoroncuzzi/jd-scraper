#!/bin/bash
# One-time (re-runnable) installer for the macOS "tailor:" URL handler app.
# Builds ~/Applications/Tailor.app; clicking a tailor: link runs tailor.py --uri.
set -euo pipefail

REPO="/Users/enricoroncuzzi/Desktop/raw/projects/jd-scraper"
APP="$HOME/Applications/Tailor.app"
WORK="$(mktemp -d)"
SCPT="$WORK/tailor-handler.applescript"

# The handler hands the command to Terminal instead of running it directly.
# Full Disk Access granted to an AppleScript applet does NOT propagate to the
# shell subprocess it spawns (the process is denied files under ~/Desktop), but
# Terminal already holds that access — so we delegate to Terminal. Tailor.app
# then needs only a one-time "control Terminal" (Automation) approval, not FDA.
cat > "$SCPT" <<APPLESCRIPT
on open location theURL
    set repo to "$REPO"
    set cmd to "cd " & quoted form of repo & " && ./.venv/bin/python tailor.py --uri " & quoted form of theURL & " 2>&1 | tee /tmp/tailor-app.log"
    tell application "Terminal"
        activate
        do script cmd
    end tell
end open location
APPLESCRIPT

mkdir -p "$HOME/Applications"
rm -rf "$APP"
osacompile -o "$APP" "$SCPT"

PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string com.enricoroncuzzi.tailor" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string tailor" "$PLIST"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

rm -rf "$WORK"
echo "Installed $APP and registered the tailor: URL scheme."
echo "Scheme check: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleURLTypes:0:CFBundleURLSchemes:0' "$PLIST")"

echo ""
echo "=============================================================="
echo "HOW IT WORKS: Tailor.app hands the job to Terminal (which already"
echo "has file access), so Tailor.app itself needs NO Full Disk Access."
echo "  - On your FIRST click, macOS asks:"
echo "      \"Tailor\" wants to control \"Terminal\"  ->  click OK (one time)."
echo "  - The tailoring then runs in a Terminal window; a notification +"
echo "    Finder folder appear when it finishes (~40s)."
echo ""
echo "  You can REMOVE Tailor.app from Full Disk Access (no longer needed)."
echo "  Terminal must keep file access to ~/Desktop, which it already has."
echo "=============================================================="
