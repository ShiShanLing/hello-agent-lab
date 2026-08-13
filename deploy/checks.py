"""发布安全检查：资源路径、rsync 目标、Redis 持久化。"""

from __future__ import annotations

import json
from pathlib import Path
import re


FRONTEND_PREFIXES = {
    "frontend": "/agent/",
    "todo": "/agent/todo/",
    "admin": "/agent/admin/",
}

ALLOWED_DELETE_DESTINATIONS = {
    "/var/www/projects/agent/todo",
    "/var/www/projects/agent/admin",
    "/opt/hello-agent/src/hello_agent",
    "/opt/hello-agent/knowledge",
    "/opt/hello-agent/demo_knowledge",
    "/opt/hello-agent/skills",
    "/opt/hello-agent/deploy",
}

FORBIDDEN_DELETE_DESTINATIONS = {
    "/var/www/projects",
    "/var/www/projects/agent",
    "/opt/hello-agent",
    "/var/lib/hello-agent",
    "/",
}

ASSET_REF_RE = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
BARE_ASSETS_RE = re.compile(r"^/assets/")


class DeployCheckError(ValueError):
    """发布前检查未通过。"""


def normalize_remote_path(path: str) -> str:
    text = (path or "").strip()
    if not text:
        raise DeployCheckError("远程路径不能为空。")
    if "://" in text or text.startswith("rsync://"):
        raise DeployCheckError(f"拒绝非本地绝对路径：{path}")
    if not text.startswith("/"):
        raise DeployCheckError(f"远程路径必须是绝对路径：{path}")
    if "/../" in f"{text}/" or text.endswith("/.."):
        raise DeployCheckError(f"远程路径不能包含 ..：{path}")
    if text != "/" and text.endswith("/"):
        text = text.rstrip("/")
    return text


def rsync_is_safe(
    dest: str,
    *,
    delete: bool = False,
    excludes: list[str] | None = None,
) -> str:
    """校验 rsync 目标。主站带 --delete 时必须排除 admin/ 和 todo/。"""
    normalized = normalize_remote_path(dest)
    excludes = [item.strip().strip("/") for item in (excludes or []) if item.strip()]
    if normalized in FORBIDDEN_DELETE_DESTINATIONS and delete:
        if normalized == "/var/www/projects/agent":
            if {"admin", "todo"}.issubset(set(excludes)):
                return normalized
            raise DeployCheckError(
                "禁止对 /var/www/projects/agent 使用无排除的 --delete，必须同时排除 admin/ 和 todo/。"
            )
        raise DeployCheckError(f"禁止对 {normalized} 使用 --delete。")
    if delete and normalized not in ALLOWED_DELETE_DESTINATIONS and normalized != "/var/www/projects/agent":
        raise DeployCheckError(f"未允许的 --delete 目标：{normalized}")
    return normalized


def extract_asset_urls(html: str) -> list[str]:
    return [match.strip() for match in ASSET_REF_RE.findall(html or "") if match.strip()]


def check_dist_index(index_html: Path, app: str) -> list[str]:
    if app not in FRONTEND_PREFIXES:
        raise DeployCheckError(f"未知前端应用：{app}")
    if not index_html.is_file():
        raise DeployCheckError(f"缺少构建产物：{index_html}")
    html = index_html.read_text(encoding="utf-8")
    urls = extract_asset_urls(html)
    prefix = FRONTEND_PREFIXES[app]
    script_or_style = [
        url
        for url in urls
        if url.endswith((".js", ".css")) or "/assets/" in url
    ]
    if not script_or_style:
        raise DeployCheckError(f"{index_html} 没有 JS/CSS 资源引用，禁止发布。")
    problems: list[str] = []
    for url in script_or_style:
        if BARE_ASSETS_RE.match(url):
            problems.append(f"根路径资源 {url}")
            continue
        if url.startswith("/") and not url.startswith(prefix):
            problems.append(f"{url} 未使用 {prefix}")
    if problems:
        raise DeployCheckError(
            f"{app} 构建资源路径错误，禁止发布：" + "；".join(problems)
        )
    return script_or_style


def parse_redis_persistence(info_text: str, save_value: str, appendonly: str) -> dict[str, object]:
    info: dict[str, str] = {}
    for line in (info_text or "").splitlines():
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        info[key.strip()] = value.strip()
    save = (save_value or "").strip()
    aof_enabled = (appendonly or "").strip().lower() in {"yes", "true", "1"}
    rdb_enabled = bool(save) and save != '""'
    last_save_ok = info.get("rdb_last_bgsave_status", "ok") != "err"
    if not rdb_enabled and not aof_enabled:
        raise DeployCheckError("Redis 未配置 RDB save 也未开启 AOF，发布会有丢数据风险。")
    if not last_save_ok:
        raise DeployCheckError("Redis 最近一次 RDB 持久化失败。")
    return {
        "ok": True,
        "aof_enabled": aof_enabled,
        "rdb_save": save,
        "rdb_last_bgsave_status": info.get("rdb_last_bgsave_status", ""),
        "aof_last_bgrewrite_status": info.get("aof_last_bgrewrite_status", ""),
        "rdb_last_save_time": info.get("rdb_last_save_time", ""),
    }


def list_release_records(releases_dir: Path, limit: int = 30) -> list[dict[str, object]]:
    log_path = Path(releases_dir) / "log.jsonl"
    if not log_path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return list(reversed(rows[-max(1, limit) :]))


SNAPSHOT_MARKERS = {
    "frontend": "index.html",
    "todo": "index.html",
    "admin": "index.html",
    "backend": "api.py",
    "knowledge": "",
    "skills": "",
}


def snapshot_marker(target: str) -> str:
    if target not in SNAPSHOT_MARKERS:
        raise DeployCheckError(f"未知快照目标：{target}")
    return SNAPSHOT_MARKERS[target]


def sibling_index_paths(agent_root: str = "/var/www/projects/agent") -> dict[str, str]:
    root = normalize_remote_path(agent_root)
    return {
        "frontend": f"{root}/index.html",
        "todo": f"{root}/todo/index.html",
        "admin": f"{root}/admin/index.html",
    }
