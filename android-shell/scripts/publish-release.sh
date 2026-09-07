#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPOSITORY="${GITHUB_REPO:-ShiShanLing/hello-agent-lab}"
HOST="${UPDATE_HOST:-baidu-bcc}"
REMOTE_DIR="${UPDATE_REMOTE_DIR:-/var/www/hello-agent-app}"
VERSION_URL="${VERSION_URL:-https://shishanling.cn/hello-agent-app/version.json}"
GRADLE_FILE="$ROOT/app/build.gradle.kts"
CHANGELOG="$ROOT/CHANGELOG.md"

VERSION_CODE="$(sed -n 's/.*versionCode = \([0-9][0-9]*\).*/\1/p' "$GRADLE_FILE" | head -n 1)"
VERSION_NAME="$(sed -n 's/.*versionName = "\([^"]*\)".*/\1/p' "$GRADLE_FILE" | head -n 1)"
if [[ -z "$VERSION_CODE" || -z "$VERSION_NAME" ]]; then
    echo "无法读取 Android 版本号" >&2
    exit 1
fi

RELEASE_NOTES="$(awk -v version="$VERSION_NAME" '
    index($0, "## [" version "]") == 1 { capture = 1; next }
    capture && /^## \[/ { exit }
    capture { print }
' "$CHANGELOG")"
if ! grep -q '[^[:space:]]' <<<"$RELEASE_NOTES"; then
    echo "CHANGELOG.md 中缺少 $VERSION_NAME 的更新记录" >&2
    exit 1
fi

if ! git -C "$ROOT/.." diff --quiet -- android-shell || \
   ! git -C "$ROOT/.." diff --cached --quiet -- android-shell; then
    echo "android-shell 存在未提交修改，请先提交再发布 Release" >&2
    exit 1
fi

"$ROOT/gradlew" -p "$ROOT" :app:testDebugUnitTest :app:assembleDebug
SOURCE_APK="$ROOT/app/build/outputs/apk/debug/app-debug.apk"
TAG="android-shell-v$VERSION_NAME"
ASSET_NAME="hello-agent-shell-$VERSION_NAME.apk"
APK_URL="https://github.com/$REPOSITORY/releases/download/$TAG/$ASSET_NAME"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
cp "$SOURCE_APK" "$STAGING/$ASSET_NAME"

if gh release view "$TAG" --repo "$REPOSITORY" >/dev/null 2>&1; then
    gh release upload "$TAG" "$STAGING/$ASSET_NAME" --repo "$REPOSITORY" --clobber
    gh release edit "$TAG" --repo "$REPOSITORY" \
        --title "服务器项目 $VERSION_NAME" --notes "$RELEASE_NOTES"
else
    gh release create "$TAG" "$STAGING/$ASSET_NAME" --repo "$REPOSITORY" \
        --target master --title "服务器项目 $VERSION_NAME" --notes "$RELEASE_NOTES"
fi

python3 - "$STAGING/version.json" "$VERSION_CODE" "$VERSION_NAME" "$APK_URL" "$RELEASE_NOTES" "$ROOT/config/modules.json" <<'PY'
import json
import sys

path, version_code, version_name, apk_url, release_notes, modules_path = sys.argv[1:]
with open(modules_path, encoding="utf-8") as handle:
    modules_document = json.load(handle)
payload = {
    "versionCode": int(version_code),
    "versionName": version_name,
    "apkUrl": apk_url,
    "forceUpdate": False,
    "releaseNotes": release_notes.strip(),
    "modules": modules_document["modules"],
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

scp -q "$STAGING/version.json" "$ROOT/config/modules.json" "$HOST:$REMOTE_DIR/"
ssh "$HOST" "chmod 644 '$REMOTE_DIR/version.json' '$REMOTE_DIR/modules.json'"
curl --fail --silent --show-error "$VERSION_URL" >/dev/null

echo "已发布服务器项目 ${VERSION_NAME}（versionCode ${VERSION_CODE}）"
echo "  GitHub APK: $APK_URL"
echo "  更新清单: $VERSION_URL"
echo "  入口配置: $REMOTE_DIR/modules.json（写入 version.json.modules，现有 Nginx 即可读取）"
