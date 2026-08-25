#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Qwen Harness"
PRODUCT="QwenHarnessDesktop"
APP_DIR="$ROOT/dist/$APP_NAME.app"
BUNDLE_ID="local.qwen-harness.desktop"
LOGO_PNG="$ROOT/AppLogo.png"
MODE="${1:-run}"
CONFIG="release"

case "$MODE" in
  run|--debug|debug|--logs|logs|--telemetry|telemetry|--verify|verify) ;;
  *) echo "usage: $0 [run|--debug|--logs|--telemetry|--verify]" >&2; exit 2 ;;
esac

cd "$ROOT"

if [[ "$MODE" == "--debug" || "$MODE" == "debug" ]]; then
  CONFIG="debug"
fi

if pgrep -x "$PRODUCT" >/dev/null 2>&1; then
  pkill -x "$PRODUCT" || true
fi
pkill -f "gui/server.py" >/dev/null 2>&1 || true

swift build -c "$CONFIG"

BIN="$(swift build -c "$CONFIG" --show-bin-path)/$PRODUCT"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$BIN" "$APP_DIR/Contents/MacOS/$PRODUCT"
chmod +x "$APP_DIR/Contents/MacOS/$PRODUCT"
printf '%s\n' "$ROOT" > "$APP_DIR/Contents/Resources/repo-root.txt"

if [[ -f "$LOGO_PNG" ]]; then
  ICONSET="$ROOT/dist/AppIcon.iconset"
  ICON_SOURCE="$ROOT/dist/AppIconSource.png"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  python3 "$ROOT/script/render_app_icon.py" "$LOGO_PNG" "$ICON_SOURCE"
  sips -z 16 16 "$ICON_SOURCE" --out "$ICONSET/icon_16x16.png" >/dev/null
  sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
  sips -z 32 32 "$ICON_SOURCE" --out "$ICONSET/icon_32x32.png" >/dev/null
  sips -z 64 64 "$ICON_SOURCE" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
  sips -z 128 128 "$ICON_SOURCE" --out "$ICONSET/icon_128x128.png" >/dev/null
  sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
  sips -z 256 256 "$ICON_SOURCE" --out "$ICONSET/icon_256x256.png" >/dev/null
  sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
  sips -z 512 512 "$ICON_SOURCE" --out "$ICONSET/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$ICON_SOURCE" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
  iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns"
fi

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>$PRODUCT</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_NAME</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIconName</key>
  <string>AppIcon</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundleVersion</key>
  <string>$(date -u +%Y%m%d%H%M)</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
  <key>NSDesktopFolderUsageDescription</key>
  <string>Qwen Harness accesses project folders you explicitly choose.</string>
  <key>NSDocumentsFolderUsageDescription</key>
  <string>Qwen Harness accesses project documents you explicitly choose.</string>
  <key>NSDownloadsFolderUsageDescription</key>
  <string>Qwen Harness imports files you explicitly choose.</string>
</dict>
</plist>
PLIST

plutil -lint "$APP_DIR/Contents/Info.plist" >/dev/null
codesign --force --deep --sign - "$APP_DIR" >/dev/null
codesign --verify --deep --strict "$APP_DIR"

open_app() {
  /usr/bin/open -n "$APP_DIR"
}

case "$MODE" in
  run)
    open_app
    ;;
  --debug|debug)
    lldb -- "$APP_DIR/Contents/MacOS/$PRODUCT"
    ;;
  --logs|logs)
    open_app
    /usr/bin/log stream --info --style compact --predicate "process == \"$PRODUCT\""
    ;;
  --telemetry|telemetry)
    open_app
    /usr/bin/log stream --info --style compact --predicate "subsystem == \"$BUNDLE_ID\""
    ;;
  --verify|verify)
    open_app
    sleep 2
    pgrep -x "$PRODUCT" >/dev/null
    echo "$APP_NAME launched from $APP_DIR"
    ;;
esac
