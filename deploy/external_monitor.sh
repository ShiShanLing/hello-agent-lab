#!/bin/bash
set -u

PATH=/usr/bin:/bin:/usr/sbin:/sbin
OPS_STATE="$HOME/Library/Application Support/HelloAgentOps/state"
OFFSITE_SUCCESS="$HOME/Backups/shishanling.cn/.state/last-success"
STATUS_FILE="$OPS_STATE/external-monitor-status.txt"
SIGNATURE_FILE="$OPS_STATE/external-monitor-signature"
FAILURES=""
URLS=(
  "https://shishanling.cn/angular20/"
  "https://shishanling.cn/agent/"
  "https://shishanling.cn/agent/todo/"
  "https://shishanling.cn/agent/admin/"
  "https://shishanling.cn/agent/api/health"
  "https://shishanling.cn/rss/"
  "https://shishanling.cn/hello-agent-app/version.json"
)

mkdir -p "$OPS_STATE"
chmod 700 "$OPS_STATE"

for url in "${URLS[@]}"; do
  code=$(/usr/bin/curl --max-time 15 --silent --show-error --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null || true)
  if [[ ! "$code" =~ ^(2|3)[0-9][0-9]$ ]]; then
    FAILURES="${FAILURES}${url} 返回 ${code:-连接失败}\n"
  fi
done

if [ ! -f "$OFFSITE_SUCCESS" ]; then
  FAILURES="${FAILURES}Mac 异地备份尚未成功\n"
else
  now=$(/bin/date +%s)
  modified=$(/usr/bin/stat -f %m "$OFFSITE_SUCCESS" 2>/dev/null || echo 0)
  age_hours=$(( (now - modified) / 3600 ))
  if [ "$age_hours" -gt 36 ]; then
    FAILURES="${FAILURES}Mac 异地备份已超过 ${age_hours} 小时未成功\n"
  fi
fi

# Keep the notification signature stable while a backup remains stale. The
# human-readable hour count may change on every run, but that is still the
# same incident and should not create another notification.
signature_input=$(printf '%b' "$FAILURES" | /usr/bin/sed -E 's/Mac 异地备份已超过 [0-9]+ 小时未成功/Mac 异地备份已超过阈值未成功/g')
signature=$(printf '%s' "$signature_input" | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')
previous=$(cat "$SIGNATURE_FILE" 2>/dev/null || true)

if [ "$signature" != "$previous" ]; then
  if [ -n "$FAILURES" ]; then
    /usr/bin/osascript -e 'display notification "公开站点或异地备份检查失败，请查看监控日志。" with title "shishanling.cn 监控告警"' >/dev/null 2>&1 || true
  elif [ -n "$previous" ]; then
    /usr/bin/osascript -e 'display notification "公开站点与异地备份均已恢复正常。" with title "shishanling.cn 已恢复"' >/dev/null 2>&1 || true
  fi
fi

printf '%s\n' "$signature" > "$SIGNATURE_FILE"
{
  /bin/date -Iseconds
  if [ -n "$FAILURES" ]; then
    printf 'FAILED\n%b' "$FAILURES"
  else
    printf 'OK\n'
  fi
} > "$STATUS_FILE"
chmod 600 "$SIGNATURE_FILE" "$STATUS_FILE"

if [ -n "$FAILURES" ]; then
  printf '%b' "$FAILURES" >&2
  exit 1
fi
echo "external checks passed"
