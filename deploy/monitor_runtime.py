#!/usr/bin/env python3
"""Monitor production services, backups, storage, HTTPS and certificate expiry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import urllib.request


DEFAULT_STATE_DIR = Path("/var/lib/hello-agent/monitor")
DEFAULT_BACKUP_DIR = Path("/var/lib/hello-agent/backups/daily")
DEFAULT_ANGULAR_BACKUP_DIR = Path("/root/backups")
DEFAULT_FRESHRSS_BACKUP_DIR = Path("/var/lib/freshrss/backups/daily")
DEFAULT_SERVICES = (
    "nginx",
    "docker",
    "fail2ban",
    "redis-server",
    "hello-agent",
    "hello-agent-worker",
    "hello-agent-scheduler",
    "nest-server",
    "bcm-agent",
)
DEFAULT_URLS = (
    "https://shishanling.cn/workshop/",
    "https://shishanling.cn/agent/",
    "https://shishanling.cn/agent/todo/",
    "https://shishanling.cn/agent/admin/",
    "https://shishanling.cn/agent/api/health",
    "https://shishanling.cn/rss/",
    "https://shishanling.cn/hello-agent-app/version.json",
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run(command: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def check_services(names: tuple[str, ...]) -> list[Check]:
    checks: list[Check] = []
    for name in names:
        result = run(["systemctl", "is-active", name])
        status = result.stdout.strip() or result.stderr.strip() or f"exit={result.returncode}"
        checks.append(Check(f"service:{name}", result.returncode == 0 and status == "active", status))
    return checks


def check_urls(urls: tuple[str, ...]) -> list[Check]:
    checks: list[Check] = []
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "hello-agent-monitor/1.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                status = response.status
            checks.append(Check(f"url:{url}", 200 <= status < 400, f"HTTP {status}"))
        except Exception as error:  # network/SSL errors are reported as one concise check
            checks.append(Check(f"url:{url}", False, f"{type(error).__name__}: {error}"))
    return checks


def latest_path(root: Path, pattern: str, *, directories: bool = False) -> Path | None:
    if not root.is_dir():
        return None
    matches = [item for item in root.glob(pattern) if item.is_dir() == directories]
    return max(matches, key=lambda item: item.stat().st_mtime, default=None)


def age_hours(path: Path) -> float:
    return max(0.0, (datetime.now().timestamp() - path.stat().st_mtime) / 3600)


def check_hello_backup(root: Path, max_age_hours: float) -> Check:
    candidates = sorted(item for item in root.glob("*") if item.is_dir()) if root.is_dir() else []
    latest = candidates[-1] if candidates else None
    if latest is None:
        return Check("backup:hello-agent", False, f"no backup under {root}")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        try:
            from deploy.backup_runtime import verify_backup
        except ModuleNotFoundError:
            from backup_runtime import verify_backup

        manifest = verify_backup(latest)
        created = datetime.fromisoformat(str(manifest["created_at"]))
        age = max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600)
        if age > max_age_hours:
            return Check("backup:hello-agent", False, f"latest={latest.name}, age={age:.1f}h")
        if not manifest.get("database") or not manifest.get("redis"):
            return Check("backup:hello-agent", False, f"latest={latest.name}, incomplete manifest")
        return Check("backup:hello-agent", True, f"latest={latest.name}, age={age:.1f}h, verified")
    except Exception as error:
        return Check("backup:hello-agent", False, f"latest={latest.name}, verify failed: {error}")


def check_angular_backup(root: Path, max_age_hours: float) -> Check:
    latest = latest_path(root, "app_db_*.db.gz")
    if latest is None:
        return Check("backup:angular20", False, f"no backup under {root}")
    age = age_hours(latest)
    if age > max_age_hours:
        return Check("backup:angular20", False, f"latest={latest.name}, age={age:.1f}h")
    result = run(["gzip", "-t", str(latest)])
    return Check(
        "backup:angular20",
        result.returncode == 0,
        f"latest={latest.name}, age={age:.1f}h, " + ("verified" if result.returncode == 0 else "gzip damaged"),
    )


def check_freshrss_backup(root: Path, max_age_hours: float) -> Check:
    candidates = sorted(item for item in root.glob("*") if item.is_dir()) if root.is_dir() else []
    latest = candidates[-1] if candidates else None
    if latest is None:
        return Check("backup:freshrss", False, f"no backup under {root}")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        try:
            from deploy.backup_freshrss import verify_backup
        except ModuleNotFoundError:
            from backup_freshrss import verify_backup

        manifest = verify_backup(latest)
        created = datetime.fromisoformat(str(manifest["created_at"]))
        age = max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600)
        if age > max_age_hours:
            return Check("backup:freshrss", False, f"latest={latest.name}, age={age:.1f}h")
        return Check("backup:freshrss", True, f"latest={latest.name}, age={age:.1f}h, verified")
    except Exception as error:
        return Check("backup:freshrss", False, f"latest={latest.name}, verify failed: {error}")


def check_freshrss_container() -> Check:
    result = run(["docker", "inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", "freshrss"])
    detail = result.stdout.strip() or result.stderr.strip() or f"exit={result.returncode}"
    return Check("container:freshrss", result.returncode == 0 and detail == "running healthy", detail)


def check_fail2ban_jail() -> Check:
    result = run(["fail2ban-client", "status", "sshd"])
    output = result.stdout.strip()
    if result.returncode != 0:
        return Check("security:fail2ban-sshd", False, result.stderr.strip() or output or f"exit={result.returncode}")
    details: list[str] = []
    for line in output.splitlines():
        clean = line.strip().lstrip("|`- ").strip()
        if clean.startswith(("Currently failed:", "Currently banned:", "Total banned:")):
            details.append(clean)
    return Check("security:fail2ban-sshd", True, ", ".join(details) or "jail active")


def check_angular_database(path: Path) -> Check:
    if not path.is_file():
        return Check("database:angular20", False, f"missing {path}")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        ok = bool(result and result[0] == "ok")
        return Check("database:angular20", ok, result[0] if result else "no result")
    except sqlite3.Error as error:
        return Check("database:angular20", False, str(error))


def check_disk(path: Path, warn_percent: float) -> Check:
    usage = shutil.disk_usage(path)
    percent = usage.used * 100 / usage.total
    return Check("disk:root", percent < warn_percent, f"used={percent:.1f}%, free={usage.free // (1024**3)}GiB")


def check_certificate(hostname: str, warn_days: int) -> Check:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=hostname) as connection:
                certificate = connection.getpeercert()
        expires = datetime.strptime(certificate["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        remaining = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
        return Check("certificate:domain", remaining >= warn_days, f"expires={expires.date()}, remaining={remaining:.1f}d")
    except Exception as error:
        return Check("certificate:domain", False, f"{type(error).__name__}: {error}")


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def notify_if_changed(state_dir: Path, checks: list[Check]) -> str:
    failures = [check for check in checks if not check.ok]
    signature = hashlib.sha256("\n".join(f"{item.name}:{item.detail}" for item in failures).encode()).hexdigest()
    state_path = state_dir / "notification-state.json"
    previous: dict[str, object] = {}
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    previous_signature = str(previous.get("signature", ""))
    previous_failed = bool(previous.get("failed", False))
    current_failed = bool(failures)
    notification = "unchanged"
    if signature != previous_signature or current_failed != previous_failed:
        recipient = os.getenv("HELLO_AGENT_MONITOR_EMAIL", "").strip() or os.getenv("ADMIN_RECOVERY_EMAIL", "").strip()
        if recipient:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
                from hello_agent.mail import send_email

                if current_failed:
                    subject = "[故障] shishanling.cn 服务器监控告警"
                    body = "以下检查未通过：\n\n" + "\n".join(f"- {item.name}: {item.detail}" for item in failures)
                else:
                    subject = "[恢复] shishanling.cn 服务已恢复"
                    body = "所有服务器、站点、备份、磁盘与证书检查均已恢复正常。"
                send_email(recipient, subject, body)
                notification = "sent"
            except Exception as error:
                notification = f"email failed: {error}"
        else:
            notification = "email not configured"
    atomic_json(
        state_path,
        {
            "signature": signature,
            "failed": current_failed,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "notification": notification,
        },
    )
    return notification


def main() -> int:
    state_dir = Path(os.getenv("HELLO_AGENT_MONITOR_STATE_DIR", str(DEFAULT_STATE_DIR))).resolve()
    services = tuple(filter(None, os.getenv("HELLO_AGENT_MONITOR_SERVICES", ",".join(DEFAULT_SERVICES)).split(",")))
    urls = tuple(filter(None, os.getenv("HELLO_AGENT_MONITOR_URLS", ",".join(DEFAULT_URLS)).split(",")))
    backup_age = float(os.getenv("HELLO_AGENT_MONITOR_BACKUP_MAX_AGE_HOURS", "30"))
    checks = [
        *check_services(services),
        *check_urls(urls),
        check_hello_backup(Path(os.getenv("HELLO_AGENT_BACKUP_DIR", str(DEFAULT_BACKUP_DIR))), backup_age),
        check_angular_backup(Path(os.getenv("ANGULAR20_BACKUP_DIR", str(DEFAULT_ANGULAR_BACKUP_DIR))), backup_age),
        check_freshrss_backup(Path(os.getenv("FRESHRSS_BACKUP_ROOT", str(DEFAULT_FRESHRSS_BACKUP_DIR))), backup_age),
        check_freshrss_container(),
        check_fail2ban_jail(),
        check_angular_database(Path(os.getenv("ANGULAR20_DATABASE_FILE", "/var/lib/mydata/app.db"))),
        check_disk(Path("/"), float(os.getenv("HELLO_AGENT_MONITOR_DISK_WARN_PERCENT", "80"))),
        check_certificate(os.getenv("HELLO_AGENT_MONITOR_CERT_HOST", "shishanling.cn"), int(os.getenv("HELLO_AGENT_MONITOR_CERT_WARN_DAYS", "21"))),
    ]
    notification = notify_if_changed(state_dir, checks)
    payload = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": all(check.ok for check in checks),
        "notification": notification,
        "checks": [asdict(check) for check in checks],
    }
    atomic_json(state_dir / "status.json", payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
