#!/bin/bash
# One-time (re-runnable) installer for the macOS "tailor:" and "jdlabel:" URL handler app.
# Builds ~/Applications/Tailor.app; clicking a tailor: link runs tailor.py --uri, clicking
# a jdlabel: link (digest "golden" / "not for me") asks for confirmation, then runs
# label.py --uri. One applet serves both schemes: the same TCC reasoning applies and a
# second app would only duplicate the install step.
set -euo pipefail

REPO="/Users/enricoroncuzzi/Desktop/raw/projects/jd-scraper"
APP="$HOME/Applications/Tailor.app"
WORK="$(mktemp -d)"
SCPT="$WORK/tailor-handler.applescript"

# The handler writes a .command file to /tmp and `open`s it — LaunchServices then
# runs it in Terminal. This sidesteps two macOS TCC walls:
#   1. Full Disk Access on an AppleScript applet does NOT reach a shell subprocess
#      it spawns, so running python directly gets "Operation not permitted" on
#      files under ~/Desktop.
#   2. Sending Terminal an Apple Event ("do script") needs an Automation grant
#      whose consent prompt doesn't reliably fire for an unsigned applet (and
#      Automation grants can't be added manually).
# Writing /tmp needs no permission, `open` is LaunchServices (not an Apple Event),
# and Terminal — which already has file access — runs the engine. So Tailor.app
# itself needs NO Full Disk Access and NO Automation grant.
cat > "$SCPT" <<APPLESCRIPT
on open location theURL
    set repo to "$REPO"
    if theURL starts with "jdlabel:" then
        set AppleScript's text item delimiters to "/"
        set parts to text items of (text 9 thru -1 of theURL)
        set labelName to item 1 of parts
        if labelName is "not_for_me" then set labelName to "not-for-me"
        set noteName to last item of parts
        set AppleScript's text item delimiters to ""
        activate
        try
            display dialog "Are you sure you want to put this offer in the " & labelName & " table?" & return & return & noteName with title "Label offer" buttons {"Cancel", "Confirm"} default button "Cancel" cancel button "Cancel" with icon caution
        on error
            return
        end try
        set labelline to "cd " & quoted form of repo & " && ./.venv/bin/python label.py --uri " & quoted form of theURL & " 2>&1 | tee /tmp/label-app.log"
        do shell script "echo '#!/bin/bash' > /tmp/label-run.command; echo " & quoted form of labelline & " >> /tmp/label-run.command; chmod +x /tmp/label-run.command; open /tmp/label-run.command"
        return
    end if
    set runline to "cd " & quoted form of repo & " && ./.venv/bin/python tailor.py --uri " & quoted form of theURL & " 2>&1 | tee /tmp/tailor-app.log"
    do shell script "echo '#!/bin/bash' > /tmp/tailor-run.command; echo " & quoted form of runline & " >> /tmp/tailor-run.command; chmod +x /tmp/tailor-run.command; open /tmp/tailor-run.command"
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
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:1 dict" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:1:CFBundleURLName string com.enricoroncuzzi.jdlabel" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:1:CFBundleURLSchemes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:1:CFBundleURLSchemes:0 string jdlabel" "$PLIST"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

rm -rf "$WORK"
echo "Installed $APP and registered the tailor: and jdlabel: URL schemes."
echo "Scheme check: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleURLTypes:0:CFBundleURLSchemes:0' "$PLIST")"

echo ""
echo "=============================================================="
echo "HOW IT WORKS: clicking a tailor: link opens a short-lived Terminal"
echo "window that runs the tailoring (~40s), then a notification + Finder"
echo "folder appear with the 3 files."
echo ""
echo "Tailor.app needs NO permissions of its own — Terminal (which already"
echo "has file access) does the work. You can REMOVE Tailor.app from Full"
echo "Disk Access if you added it earlier; it is no longer needed."
echo "=============================================================="
