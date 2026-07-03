#!/bin/bash
# One-time (re-runnable) installer for the macOS "tailor:" URL handler app.
# Builds ~/Applications/Tailor.app; clicking a tailor: link runs tailor.py --uri.
set -euo pipefail

REPO="/Users/enricoroncuzzi/Desktop/raw/projects/jd-scraper"
APP="$HOME/Applications/Tailor.app"
WORK="$(mktemp -d)"
SCPT="$WORK/tailor-handler.applescript"

cat > "$SCPT" <<APPLESCRIPT
on open location theURL
    set repo to "$REPO"
    do shell script "cd " & quoted form of repo & " && ./.venv/bin/python tailor.py --uri " & quoted form of theURL & " > /tmp/tailor-app.log 2>&1 &"
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
