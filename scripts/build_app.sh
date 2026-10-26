#!/bin/bash
# Build "豆包视频批量生成.app" — a thin macOS launcher around launcher.py.
#
# The bundle carries the Python sources; on first launch it creates a private
# virtualenv under ~/Library/Application Support/DoubaoVideoBatch and installs
# requirements.txt there. Python 3.10+ must be installed on the Mac.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/app/__init__.py")"
OUT_DIR="${1:-$ROOT/dist}"
APP_NAME="豆包视频批量生成"
APP="$OUT_DIR/$APP_NAME.app"
BUNDLE_ID="com.seanli.doubao-video-batch"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

# --- sources -----------------------------------------------------------------
rsync -a --delete \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' \
  "$ROOT/app" "$ROOT/launcher.py" "$ROOT/stop.py" "$ROOT/requirements.txt" "$ROOT/LICENSE" \
  "$APP/Contents/Resources/app/"

# --- launcher ----------------------------------------------------------------
cat > "$APP/Contents/MacOS/launch" <<'LAUNCH'
#!/bin/bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
SUPPORT="$HOME/Library/Application Support/DoubaoVideoBatch"
mkdir -p "$SUPPORT/logs"
export DOUBAO_VIDEO_DATA="$SUPPORT/data"
export DOUBAO_VIDEO_VENV="$SUPPORT/venv"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.pyenv/shims:/usr/bin:/bin:/usr/sbin:/sbin"
unset PYTHONHOME PYTHONPATH

PYTHON_BIN=""
for candidate in \
  "$DOUBAO_VIDEO_VENV/bin/python" \
  /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.pyenv/shims/python3" \
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
  "$(command -v python3 2>/dev/null || true)" /usr/bin/python3; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] \
    && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  /usr/bin/osascript -e 'display alert "需要 Python 3.10 或更高版本" message "请先安装 Python（https://www.python.org/downloads/macos/ 或 Homebrew：brew install python），然后重新打开本应用。" as critical'
  /usr/bin/open "https://www.python.org/downloads/macos/"
  exit 1
fi

if [ ! -x "$DOUBAO_VIDEO_VENV/bin/python" ]; then
  /usr/bin/osascript -e 'display notification "首次启动：正在准备运行环境（约 1–3 分钟），完成后会自动打开页面。" with title "豆包视频批量生成"'
fi

cd "$HERE"
exec "$PYTHON_BIN" launcher.py >>"$SUPPORT/logs/launcher.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"

# --- Info.plist ----------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# --- icon (optional: needs qlmanage + iconutil, both ship with macOS) ---------
ICON_TMP="$(mktemp -d)"
cat > "$ICON_TMP/icon.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#343a44"/><stop offset="1" stop-color="#1c1f25"/></linearGradient></defs>
  <rect x="64" y="64" width="896" height="896" rx="200" fill="url(#g)"/>
  <rect x="300" y="210" width="424" height="604" rx="56" fill="none" stroke="#78c6ff" stroke-width="36"/>
  <path d="M450 400 L630 512 L450 624 Z" fill="#78c6ff"/>
</svg>
SVG
if qlmanage -t -s 1024 -o "$ICON_TMP" "$ICON_TMP/icon.svg" >/dev/null 2>&1 && [ -f "$ICON_TMP/icon.svg.png" ]; then
  ICONSET="$ICON_TMP/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$ICON_TMP/icon.svg.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$ICON_TMP/icon.svg.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns" || true
fi
rm -rf "$ICON_TMP"

# Ad-hoc signature (not notarized).
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

if [ "${ZIP:-1}" = "1" ]; then
  ZIP_PATH="$OUT_DIR/DoubaoVideoBatch-v$VERSION-macOS.zip"
  rm -f "$ZIP_PATH"
  (cd "$OUT_DIR" && ditto -c -k --sequesterRsrc --keepParent "$APP_NAME.app" "$ZIP_PATH")
  shasum -a 256 "$ZIP_PATH" | awk '{print $1}' > "$ZIP_PATH.sha256"
  echo "Built $APP"
  echo "Zipped $ZIP_PATH"
else
  echo "Built $APP"
fi
