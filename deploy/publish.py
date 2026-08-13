#!/usr/bin/env python3
"""Hello Agent Lab 发布脚本：构建、测试、备份、同步、验收、失败回滚。"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "deploy") not in sys.path:
    sys.path.insert(0, str(ROOT / "deploy"))

from checks import (  # noqa: E402
    DeployCheckError,
    check_dist_index,
    extract_asset_urls,
    parse_redis_persistence,
    rsync_is_safe,
    sibling_index_paths,
    snapshot_marker,
)

DEFAULT_HOST = os.environ.get("DEPLOY_SSH_HOST", "baidu-bcc")
DEFAULT_PUBLIC_BASE = os.environ.get("DEPLOY_PUBLIC_BASE", "http://106.13.175.227")
REMOTE_AGENT_ROOT = os.environ.get("DEPLOY_REMOTE_AGENT_ROOT", "/var/www/projects/agent")
REMOTE_BACKEND = os.environ.get("DEPLOY_REMOTE_BACKEND", "/opt/hello-agent")
REMOTE_DATA = os.environ.get("DEPLOY_REMOTE_DATA", "/var/lib/hello-agent")
REMOTE_RELEASES = os.environ.get("DEPLOY_REMOTE_RELEASES", "/var/lib/hello-agent/releases")
PYTHON = ROOT / ".venv" / "bin" / "python"

TARGETS = ("frontend", "todo", "admin", "backend", "knowledge", "skills")
FRONTEND_APPS = {
    "frontend": ROOT / "frontend",
    "todo": ROOT / "todo-frontend",
    "admin": ROOT / "admin-frontend",
}


class PublishError(RuntimeError):
    """发布流程失败。"""


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def run(
    command: list[str] | str,
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        capture_output=capture,
        env=env,
        shell=isinstance(command, str),
    )
    if check and result.returncode != 0:
        output = (result.stderr or result.stdout or "").strip()
        raise PublishError(
            f"命令失败（{result.returncode}）：{command if isinstance(command, str) else ' '.join(command)}"
            + (f"\n{output}" if output else "")
        )
    return result


def ssh(host: str, command: str, *, check: bool = True, capture: bool = True) -> str:
    result = run(["ssh", host, command], check=check, capture=capture)
    return (result.stdout or "").strip()


def git_info() -> dict[str, str]:
    sha_result = run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture=True, check=False
    )
    branch_result = run(
        ["git", "branch", "--show-current"], cwd=ROOT, capture=True, check=False
    )
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else "uncommitted"
    branch = branch_result.stdout.strip() or "HEAD"
    return {"git_sha": sha, "git_branch": branch}


def parse_targets(raw: str) -> list[str]:
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        raise PublishError("必须通过 --targets 指定要发布的应用，例如 frontend 或 backend,admin。")
    unknown = [item for item in items if item not in TARGETS]
    if unknown:
        raise PublishError(f"未知发布目标：{', '.join(unknown)}。可选：{', '.join(TARGETS)}")
    return list(dict.fromkeys(items))


def run_tests(include_backend: bool) -> dict[str, object]:
    python = str(PYTHON if PYTHON.exists() else sys.executable)
    if include_backend:
        command = [python, "-m", "unittest", "discover", "-s", "tests", "-q"]
    else:
        command = [python, "-m", "unittest", "tests.test_deploy", "-q"]
    log("运行测试：" + " ".join(command[2:]))
    run(command, cwd=ROOT)
    return {"passed": True, "backend_suite": include_backend}


def build_frontend(app: str) -> list[str]:
    source = FRONTEND_APPS[app]
    log(f"构建 {app}：{source}")
    run(["npm", "run", "build"], cwd=source)
    urls = check_dist_index(source / "dist" / "index.html", app)
    log(f"{app} 资源路径检查通过：{len(urls)} 个 JS/CSS")
    return urls


def redis_check(host: str) -> dict[str, object]:
    info = ssh(host, "redis-cli INFO persistence")
    save = ssh(host, "redis-cli CONFIG GET save")
    appendonly = ssh(host, "redis-cli CONFIG GET appendonly")
    save_value = ""
    lines = [line.strip() for line in save.splitlines() if line.strip()]
    if len(lines) >= 2:
        save_value = lines[1]
    aof_value = ""
    aof_lines = [line.strip() for line in appendonly.splitlines() if line.strip()]
    if len(aof_lines) >= 2:
        aof_value = aof_lines[1]
    parsed = parse_redis_persistence(info, save_value, aof_value)
    log(
        "Redis 持久化检查通过："
        f"RDB={parsed['rdb_save'] or 'off'} AOF={'on' if parsed['aof_enabled'] else 'off'}"
    )
    return parsed


def ensure_remote_dirs(host: str, release_id: str) -> dict[str, str]:
    release_root = f"{REMOTE_RELEASES}/{release_id}"
    snapshot = f"{release_root}/snapshot"
    ssh(
        host,
        " && ".join(
            [
                f"mkdir -p {REMOTE_DATA}/backups {snapshot}/frontend {snapshot}/todo {snapshot}/admin {snapshot}/backend {snapshot}/knowledge {snapshot}/skills",
                f"chmod 755 {REMOTE_RELEASES} {REMOTE_DATA}/backups {release_root} {snapshot}",
            ]
        ),
    )
    return {"release_root": release_root, "snapshot": snapshot}


def sqlite_backup(host: str, release_id: str) -> str:
    db_path = f"{REMOTE_DATA}/agent.db"
    backup_name = f"agent-{release_id}.db"
    release_copy = f"{REMOTE_RELEASES}/{release_id}/{backup_name}"
    rotating = f"{REMOTE_DATA}/backups/{backup_name}"
    exists = ssh(host, f"test -f {db_path} && echo yes || echo no")
    if exists != "yes":
        log(f"未找到 {db_path}，跳过 SQLite 备份")
        return ""
    log("备份 SQLite 数据库")
    ssh(
        host,
        "python3 -c \""
        "import sqlite3,pathlib,glob,os;"
        f"src=sqlite3.connect({db_path!r});"
        f"pathlib.Path({release_copy!r}).parent.mkdir(parents=True,exist_ok=True);"
        f"dst=sqlite3.connect({release_copy!r});"
        "src.backup(dst); dst.close(); src.close();"
        f"import shutil; shutil.copy2({release_copy!r},{rotating!r});"
        f"os.chmod({release_copy!r},0o644); os.chmod({rotating!r},0o644);"
        "old=sorted(glob.glob('/var/lib/hello-agent/backups/agent-*.db'),key=os.path.getmtime,reverse=True)[20:];"
        "[os.remove(item) for item in old]"
        "\"",
    )
    return release_copy


def snapshot_target(host: str, target: str, snapshot_root: str) -> None:
    log(f"快照当前 {target}")
    if target == "frontend":
        rsync_is_safe(
            f"{REMOTE_AGENT_ROOT}/",
            delete=True,
            excludes=["admin/", "todo/"],
        )
        ssh(
            host,
            " ".join(
                [
                    "rsync -a --delete",
                    "--exclude='admin/' --exclude='todo/'",
                    f"{REMOTE_AGENT_ROOT}/ {snapshot_root}/frontend/",
                ]
            ),
        )
        return
    mapping = {
        "todo": (f"{REMOTE_AGENT_ROOT}/todo/", f"{snapshot_root}/todo/"),
        "admin": (f"{REMOTE_AGENT_ROOT}/admin/", f"{snapshot_root}/admin/"),
        "backend": (f"{REMOTE_BACKEND}/src/hello_agent/", f"{snapshot_root}/backend/"),
        "knowledge": (f"{REMOTE_BACKEND}/knowledge/", f"{snapshot_root}/knowledge/"),
        "skills": (f"{REMOTE_BACKEND}/skills/", f"{snapshot_root}/skills/"),
    }
    source, dest = mapping[target]
    rsync_is_safe(source, delete=False)
    ssh(host, f"if [ -d {source} ]; then rsync -a --delete {source} {dest}; fi")


def deploy_target(host: str, target: str, *, dry_run: bool) -> None:
    dry = ["--dry-run"] if dry_run else []
    if target == "frontend":
        dest = f"{REMOTE_AGENT_ROOT}/"
        rsync_is_safe(dest, delete=True, excludes=["admin/", "todo/"])
        command = [
            "rsync",
            "-av",
            "--delete",
            "--exclude=admin/",
            "--exclude=todo/",
            *dry,
            f"{ROOT / 'frontend' / 'dist'}/",
            f"{host}:{dest}",
        ]
        log("发布主站（排除 admin/ 与 todo/）")
        run(command)
        return
    local_remote = {
        "todo": (ROOT / "todo-frontend" / "dist", f"{REMOTE_AGENT_ROOT}/todo/"),
        "admin": (ROOT / "admin-frontend" / "dist", f"{REMOTE_AGENT_ROOT}/admin/"),
        "backend": (ROOT / "src" / "hello_agent", f"{REMOTE_BACKEND}/src/hello_agent/"),
        "knowledge": (ROOT / "knowledge", f"{REMOTE_BACKEND}/knowledge/"),
        "skills": (ROOT / "skills", f"{REMOTE_BACKEND}/skills/"),
    }
    local, dest = local_remote[target]
    rsync_is_safe(dest, delete=True)
    extra = ["--exclude=__pycache__", "--exclude=*.pyc"] if target == "backend" else []
    if target == "backend":
        rsync_is_safe(f"{REMOTE_BACKEND}/deploy/", delete=True)
        run(
            [
                "rsync",
                "-av",
                "--delete",
                "--exclude=__pycache__",
                "--exclude=*.pyc",
                *dry,
                f"{ROOT / 'deploy'}/",
                f"{host}:{REMOTE_BACKEND}/deploy/",
            ]
        )
    log(f"发布 {target} -> {dest}")
    run(["rsync", "-av", "--delete", *extra, *dry, f"{local}/", f"{host}:{dest}"])


def snapshot_is_complete(host: str, target: str, snapshot_root: str) -> bool:
    marker = snapshot_marker(target)
    path = f"{snapshot_root}/{target}/{marker}".rstrip("/")
    return ssh(host, f"test -e {path} && echo yes || echo no") == "yes"


def rollback_target(host: str, target: str, snapshot_root: str) -> None:
    if not snapshot_is_complete(host, target, snapshot_root):
        log(f"跳过回滚 {target}：快照不完整，避免覆盖线上文件")
        return
    log(f"回滚 {target}")
    if target == "frontend":
        rsync_is_safe(
            f"{REMOTE_AGENT_ROOT}/",
            delete=True,
            excludes=["admin/", "todo/"],
        )
        ssh(
            host,
            " ".join(
                [
                    "rsync -a --delete --exclude='admin/' --exclude='todo/'",
                    f"{snapshot_root}/frontend/ {REMOTE_AGENT_ROOT}/",
                ]
            ),
        )
        return
    mapping = {
        "todo": (f"{snapshot_root}/todo/", f"{REMOTE_AGENT_ROOT}/todo/"),
        "admin": (f"{snapshot_root}/admin/", f"{REMOTE_AGENT_ROOT}/admin/"),
        "backend": (f"{snapshot_root}/backend/", f"{REMOTE_BACKEND}/src/hello_agent/"),
        "knowledge": (f"{snapshot_root}/knowledge/", f"{REMOTE_BACKEND}/knowledge/"),
        "skills": (f"{snapshot_root}/skills/", f"{REMOTE_BACKEND}/skills/"),
    }
    source, dest = mapping[target]
    rsync_is_safe(dest, delete=True)
    ssh(host, f"if [ -d {source} ]; then rsync -a --delete {source} {dest}; fi")


def restart_backend(host: str) -> None:
    log("重启 hello-agent（SIGKILL 避免 SSE 卡住）")
    ssh(
        host,
        "systemctl kill -s SIGKILL hello-agent || true; systemctl start hello-agent; "
        "systemctl restart hello-agent-worker || true; "
        "systemctl restart hello-agent-scheduler || true",
        capture=False,
    )
    for attempt in range(20):
        time.sleep(1)
        active = ssh(host, "systemctl is-active hello-agent || true")
        health = ssh(
            host,
            "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health || true",
        )
        if active == "active" and health == "200":
            log("hello-agent 已恢复健康")
            return
        log(f"等待服务就绪（{attempt + 1}/20）status={active} health={health}")
    raise PublishError("后端重启后健康检查未通过。")


def http_status(url: str) -> str:
    result = run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url],
        check=False,
        capture=True,
    )
    return (result.stdout or "000").strip()


def verify(host: str, public_base: str, built: dict[str, list[str]]) -> dict[str, object]:
    results: dict[str, object] = {}
    pages = {
        "/agent/": f"{public_base.rstrip('/')}/agent/",
        "/agent/todo/": f"{public_base.rstrip('/')}/agent/todo/",
        "/agent/admin/": f"{public_base.rstrip('/')}/agent/admin/",
        "/agent/api/health": f"{public_base.rstrip('/')}/agent/api/health",
    }
    for name, url in pages.items():
        code = http_status(url)
        results[name] = code
        if code != "200":
            raise PublishError(f"验收失败：{url} 返回 {code}")
    siblings = sibling_index_paths(REMOTE_AGENT_ROOT)
    for name, path in siblings.items():
        exists = ssh(host, f"test -f {path} && echo yes || echo no")
        results[f"sibling_{name}"] = exists
        if exists != "yes":
            raise PublishError(f"兄弟应用入口丢失：{path}")
    services = ssh(host, "systemctl is-active hello-agent hello-agent-worker redis-server")
    results["services"] = services.split()
    if any(item != "active" for item in results["services"]):
        raise PublishError(f"服务状态异常：{services}")
    for app, urls in built.items():
        prefix = {
            "frontend": "/agent/",
            "todo": "/agent/todo/",
            "admin": "/agent/admin/",
        }[app]
        remote_html = ssh(host, f"sed -n '1,40p' {siblings[app]}")
        remote_urls = [
            url
            for url in extract_asset_urls(remote_html)
            if url.endswith((".js", ".css"))
        ]
        for url in remote_urls or urls:
            if url.startswith("/"):
                full = f"{public_base.rstrip('/')}{url}"
            else:
                full = f"{public_base.rstrip('/')}{prefix}{url}"
            code = http_status(full)
            results[url] = code
            if code != "200":
                raise PublishError(f"静态资源验收失败：{full} 返回 {code}")
    log("发布验收通过：主站 / Todo / Admin / API 均为 200")
    return results


def write_record(host: str, record: dict[str, object]) -> None:
    encoded = base64.b64encode(
        json.dumps(record, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    ssh(
        host,
        "python3 -c \""
        "import base64,json,pathlib;"
        f"p=json.loads(base64.b64decode('{encoded}').decode());"
        f"root=pathlib.Path({REMOTE_RELEASES!r});"
        "root.mkdir(parents=True,exist_ok=True);"
        "manifest=root/p['id']/'manifest.json';"
        "manifest.parent.mkdir(parents=True,exist_ok=True);"
        "manifest.write_text(json.dumps(p,ensure_ascii=False,indent=2)+'\\n',encoding='utf-8');"
        "(root/'log.jsonl').open('a',encoding='utf-8').write(json.dumps(p,ensure_ascii=False)+'\\n')"
        "\"",
    )
    local_dir = ROOT / ".deploy-releases"
    local_dir.mkdir(exist_ok=True)
    (local_dir / f"{record['id']}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def publish(args: argparse.Namespace) -> int:
    targets = parse_targets(args.targets)
    host = args.host
    public_base = args.public_base.rstrip("/")
    started = time.time()
    release_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    info = git_info()
    release_id = f"{release_id}-{info['git_sha']}"
    record: dict[str, object] = {
        "id": release_id,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat() + "Z",
        "operator": os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown",
        "targets": targets,
        "dry_run": bool(args.dry_run),
        "status": "running",
        **info,
    }
    built: dict[str, list[str]] = {}
    snapshot_root = ""
    deployed: list[str] = []
    backend_restarted = False
    try:
        record["tests"] = run_tests(include_backend="backend" in targets or args.full_tests)
        for app in ("frontend", "todo", "admin"):
            if app in targets:
                built[app] = build_frontend(app)
        record["redis"] = redis_check(host)
        if args.dry_run:
            for target in targets:
                deploy_target(host, target, dry_run=True)
            record["status"] = "dry_run"
            record["duration_seconds"] = round(time.time() - started, 1)
            write_record(host, record)
            log(f"演练完成，未改动线上文件。发布记录 {release_id}")
            return 0
        paths = ensure_remote_dirs(host, release_id)
        snapshot_root = paths["snapshot"]
        record["backup"] = {"sqlite": sqlite_backup(host, release_id), "snapshot": snapshot_root}
        for target in targets:
            snapshot_target(host, target, snapshot_root)
        for target in targets:
            deploy_target(host, target, dry_run=False)
            deployed.append(target)
        if "backend" in deployed:
            restart_backend(host)
            backend_restarted = True
        record["verify"] = verify(host, public_base, built)
        record["status"] = "success"
        record["duration_seconds"] = round(time.time() - started, 1)
        write_record(host, record)
        log(f"发布成功 {release_id}，目标：{', '.join(targets)}")
        return 0
    except (DeployCheckError, PublishError, subprocess.CalledProcessError) as error:
        record["error"] = str(error)
        record["duration_seconds"] = round(time.time() - started, 1)
        if deployed and not args.dry_run:
            try:
                for target in reversed(deployed):
                    rollback_target(host, target, snapshot_root)
                if backend_restarted or "backend" in deployed:
                    restart_backend(host)
                record["status"] = "rolled_back"
                log("已回滚到发布前快照")
            except Exception as rollback_error:  # noqa: BLE001
                record["status"] = "failed"
                record["rollback_error"] = str(rollback_error)
                log(f"回滚失败：{rollback_error}")
        else:
            record["status"] = "failed"
        try:
            write_record(host, record)
        except Exception as write_error:  # noqa: BLE001
            log(f"写入发布记录失败：{write_error}")
        log(f"发布失败：{error}")
        return 1


def show_history(host: str, limit: int) -> int:
    output = ssh(
        host,
        f"python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "import json\n"
        f"log = Path({REMOTE_RELEASES!r}) / 'log.jsonl'\n"
        "if not log.exists():\n"
        "    print('[]')\n"
        "    raise SystemExit\n"
        "lines = [line for line in log.read_text(encoding='utf-8').splitlines() if line.strip()]\n"
        f"rows = [json.loads(line) for line in lines[-{limit}:]]\n"
        "print(json.dumps(list(reversed(rows)), ensure_ascii=False, indent=2))\n"
        "PY",
    )
    print(output or "[]")
    return 0


def check_local_dist() -> int:
    for app, source in FRONTEND_APPS.items():
        index = source / "dist" / "index.html"
        if not index.exists():
            log(f"跳过 {app}：尚未构建")
            continue
        urls = check_dist_index(index, app)
        log(f"{app} OK：{', '.join(urls)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hello Agent Lab 自动发布")
    parser.add_argument("--host", default=DEFAULT_HOST, help="SSH 主机，默认 baidu-bcc")
    parser.add_argument("--public-base", default=DEFAULT_PUBLIC_BASE, help="公网验收入口")
    sub = parser.add_subparsers(dest="command", required=True)
    publish_cmd = sub.add_parser("publish", help="构建、测试、备份并发布指定应用")
    publish_cmd.add_argument(
        "--targets",
        required=True,
        help="逗号分隔：frontend,todo,admin,backend,knowledge,skills",
    )
    publish_cmd.add_argument("--dry-run", action="store_true", help="只演练，不改线上文件")
    publish_cmd.add_argument("--full-tests", action="store_true", help="即使只发前端也跑完整后端测试")
    history_cmd = sub.add_parser("history", help="查看服务器上的发布记录")
    history_cmd.add_argument("--limit", type=int, default=20)
    sub.add_parser("check-dist", help="检查本地 dist 资源路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            return publish(args)
        if args.command == "history":
            return show_history(args.host, args.limit)
        if args.command == "check-dist":
            return check_local_dist()
        parser.error("未知命令")
        return 2
    except (DeployCheckError, PublishError) as error:
        log(str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
