#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="审稿小工具"
APP_DIR="$ROOT/dist/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
APP_TOOL="$RESOURCES/reviewer_tool"
PYTHON_DEFAULT="/Users/junchai/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

rm -rf "$APP_DIR"
mkdir -p "$MACOS" "$RESOURCES"

rsync -a \
  --exclude 'work/' \
  --exclude '__pycache__/' \
  "$ROOT/reviewer_tool/" "$APP_TOOL/"

/bin/cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleExecutable</key>
  <string>reviewer-tool-launcher</string>
  <key>CFBundleIdentifier</key>
  <string>com.local.paper-reviewer-tool</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
</dict>
</plist>
PLIST

/bin/cat > "$MACOS/reviewer-tool-launcher" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOL_ROOT="$APP_ROOT/Resources/reviewer_tool"
LOG_DIR="$HOME/Library/Logs/ReviewerTool"
LOG_FILE="$LOG_DIR/server.log"
PORT="${PORT:-8765}"
PYTHON_DEFAULT="/Users/junchai/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

mkdir -p "$LOG_DIR"

if [[ -x "$PYTHON_DEFAULT" ]]; then
  PYTHON="$PYTHON_DEFAULT"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  osascript -e 'display alert "审稿小工具无法启动" message "没有找到可用的 Python 3。请先安装 Python 3 或从 Codex 内启动服务。"'
  exit 1
fi

if ! curl --noproxy '*' -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
  cd "$TOOL_ROOT"
  nohup env PORT="$PORT" "$PYTHON" server.py >> "$LOG_FILE" 2>&1 &
fi

for _ in {1..30}; do
  if curl --noproxy '*' -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT/"
    exit 0
  fi
  sleep 0.5
done

osascript -e "display alert \"审稿小工具启动超时\" message \"请查看日志：$LOG_FILE\""
exit 1
LAUNCHER

chmod +x "$MACOS/reviewer-tool-launcher"

echo "$APP_DIR"
