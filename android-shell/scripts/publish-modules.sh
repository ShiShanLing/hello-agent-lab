#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${UPDATE_HOST:-baidu-bcc}"
REMOTE_DIR="${UPDATE_REMOTE_DIR:-/var/www/hello-agent-app}"
VERSION_URL="${VERSION_URL:-https://shishanling.cn/hello-agent-app/version.json}"
MODULES_FILE="$ROOT/config/modules.json"

if [[ ! -f "$MODULES_FILE" ]]; then
  echo "缺少 $MODULES_FILE" >&2
  exit 1
fi

python3 - "$MODULES_FILE" >/dev/null <<'PY'
import json, sys
from pathlib import Path
doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert isinstance(doc.get("modules"), list) and doc["modules"], "modules 为空"
for item in doc["modules"]:
    assert item["id"] and item["title"] and item["url"].startswith("https://shishanling.cn/")
PY

scp -q "$MODULES_FILE" "$HOST:$REMOTE_DIR/modules.json"

python3 - "$MODULES_FILE" "$HOST" "$REMOTE_DIR" <<'PY'
import json, subprocess, sys
from pathlib import Path

modules_path, host, remote_dir = sys.argv[1:]
modules = json.loads(Path(modules_path).read_text(encoding="utf-8"))["modules"]
current = subprocess.check_output(
    ["ssh", host, f"cat {remote_dir}/version.json"],
    text=True,
)
payload = json.loads(current)
payload["modules"] = modules
staging = Path("/tmp/hello-agent-app-version.json")
staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
subprocess.check_call(["scp", "-q", str(staging), f"{host}:{remote_dir}/version.json"])
staging.unlink(missing_ok=True)
PY

ssh "$HOST" "chmod 644 '$REMOTE_DIR/version.json' '$REMOTE_DIR/modules.json'"
curl --fail --silent --show-error "$VERSION_URL" >/dev/null
echo "已更新入口配置。之后改 android-shell/config/modules.json 再跑本脚本即可，不必重打 APK。"
echo "  $VERSION_URL"
