"""隐私安全的系统管理：只返回聚合指标和账号权限元数据。"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from sqlalchemy import case, delete, func, select

from hello_agent.database import (
    AdminAuditRecord,
    AdminLoginChallengeRecord,
    AdminSessionRecord,
    AdminUserRecord,
    AgentRunRecord,
    EvaluationRunRecord,
    KnowledgeDocumentRecord,
    LoginSessionRecord,
    UserRecord,
    _create_session_factory,
)


ADMIN_ROLES = {"knowledge_manager", "member"}
DEFAULT_RELEASES_DIR = Path("/var/lib/hello-agent/releases")


def list_release_records(
    releases_dir: Path | None = None, limit: int = 30
) -> list[dict[str, object]]:
    root = Path(
        releases_dir
        or os.getenv("HELLO_AGENT_RELEASES_DIR", str(DEFAULT_RELEASES_DIR))
    )
    log_path = root / "log.jsonl"
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
            public = {
                "id": item.get("id"),
                "created_at": item.get("created_at"),
                "status": item.get("status"),
                "targets": item.get("targets") or [],
                "git_sha": item.get("git_sha"),
                "git_branch": item.get("git_branch"),
                "operator": item.get("operator"),
                "duration_seconds": item.get("duration_seconds"),
                "error": item.get("error"),
                "dry_run": bool(item.get("dry_run")),
            }
            rows.append(public)
    return list(reversed(rows[-max(1, limit) :]))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SqliteAdminService:
    """后台统计永不读取聊天、Todo、旅行计划和文档正文。"""

    def __init__(self, database_path: Path) -> None:
        self._session_factory = _create_session_factory(
            str(database_path.resolve())
        )

    def overview(self) -> dict[str, object]:
        now = _utc_now()
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)
        with self._session_factory() as session:
            total_users = session.scalar(select(func.count(UserRecord.id))) or 0
            active_users = session.scalar(
                select(func.count(UserRecord.id)).where(UserRecord.is_active.is_(True))
            ) or 0
            role_rows = session.execute(
                select(UserRecord.role, func.count(UserRecord.id)).group_by(
                    UserRecord.role
                )
            ).all()

            run_stats = session.execute(
                select(
                    func.count(AgentRunRecord.id),
                    func.sum(
                        case((AgentRunRecord.status == "success", 1), else_=0)
                    ),
                    func.sum(AgentRunRecord.total_tokens),
                    func.avg(AgentRunRecord.duration_ms),
                    func.sum(AgentRunRecord.estimated_cost),
                ).where(AgentRunRecord.started_at >= day_ago)
            ).one()
            knowledge_stats = session.execute(
                select(
                    func.count(KnowledgeDocumentRecord.id),
                    func.sum(KnowledgeDocumentRecord.chunk_count),
                    func.sum(KnowledgeDocumentRecord.size_bytes),
                )
            ).one()
            evaluation_stats = session.execute(
                select(
                    func.count(EvaluationRunRecord.id),
                    func.avg(EvaluationRunRecord.score),
                ).where(EvaluationRunRecord.started_at >= week_ago)
            ).one()
            active_sessions = session.scalar(
                select(func.count(LoginSessionRecord.token_hash)).where(
                    LoginSessionRecord.expires_at > now
                )
            ) or 0

            activity_rows = session.execute(
                select(
                    func.date(AgentRunRecord.started_at),
                    func.count(AgentRunRecord.id),
                )
                .where(AgentRunRecord.started_at >= week_ago)
                .group_by(func.date(AgentRunRecord.started_at))
            ).all()

        run_count = int(run_stats[0] or 0)
        success_count = int(run_stats[1] or 0)
        activity_map = {str(day): int(count) for day, count in activity_rows}
        activity = []
        for days_before in range(6, -1, -1):
            day = (now - timedelta(days=days_before)).date().isoformat()
            activity.append({"date": day, "runs": activity_map.get(day, 0)})

        return {
            "users": {
                "total": int(total_users),
                "active": int(active_users),
                "roles": {str(role): int(count) for role, count in role_rows},
                "active_sessions": int(active_sessions),
            },
            "runs_24h": {
                "total": run_count,
                "success": success_count,
                "success_rate": (
                    round(success_count / run_count * 100, 1) if run_count else 0
                ),
                "total_tokens": int(run_stats[2] or 0),
                "average_duration_ms": round(float(run_stats[3] or 0)),
                "estimated_cost": round(float(run_stats[4] or 0), 6),
            },
            "knowledge": {
                "documents": int(knowledge_stats[0] or 0),
                "chunks": int(knowledge_stats[1] or 0),
                "size_bytes": int(knowledge_stats[2] or 0),
            },
            "evaluations_7d": {
                "runs": int(evaluation_stats[0] or 0),
                "average_score": round(float(evaluation_stats[1] or 0), 1),
            },
            "activity_7d": activity,
            "generated_at": now.isoformat(),
            "privacy_notice": (
                "统计数据不包含聊天内容、Todo、旅行计划、用户问题或私人文档名称。"
            ),
        }

    def list_users(self) -> list[dict[str, object]]:
        with self._session_factory() as session:
            users = session.scalars(
                select(UserRecord).order_by(UserRecord.created_at.desc())
            ).all()
            return [self._user_dict(user) for user in users]

    def list_admin_users(self) -> list[dict[str, object]]:
        with self._session_factory() as session:
            admins = session.scalars(
                select(AdminUserRecord).order_by(AdminUserRecord.created_at.desc())
            ).all()
            return [self._admin_dict(admin) for admin in admins]

    def update_user(
        self,
        actor_user_id: str,
        target_user_id: str,
        role: str,
        is_active: bool,
        ip_address: str | None,
    ) -> dict[str, object]:
        if role not in ADMIN_ROLES:
            raise ValueError("不支持的用户角色。")
        with self._session_factory.begin() as session:
            target = session.get(UserRecord, target_user_id)
            if target is None:
                raise ValueError("找不到指定用户。")
            previous = {"role": target.role, "is_active": bool(target.is_active)}
            changed = previous != {"role": role, "is_active": is_active}
            target.role = role
            target.is_active = is_active
            if changed:
                session.execute(
                    delete(LoginSessionRecord).where(
                        LoginSessionRecord.user_id == target_user_id
                    )
                )
                session.add(
                    AdminAuditRecord(
                        actor_user_id=actor_user_id,
                        action="user_permission_updated",
                        target_user_id=target_user_id,
                        detail_json=json.dumps(
                            {
                                "before": previous,
                                "after": {"role": role, "is_active": is_active},
                            },
                            ensure_ascii=False,
                        ),
                        ip_address=(ip_address or "")[:64] or None,
                        created_at=_utc_now(),
                    )
                )
            session.flush()
            return self._user_dict(target)

    def list_audit_logs(self, limit: int = 100) -> list[dict[str, object]]:
        with self._session_factory() as session:
            logs = session.scalars(
                select(AdminAuditRecord)
                .order_by(AdminAuditRecord.created_at.desc())
                .limit(max(1, min(limit, 200)))
            ).all()
            user_ids = {
                user_id
                for log in logs
                for user_id in (log.actor_user_id, log.target_user_id)
                if user_id
            }
            target_users = {
                user.id: user
                for user in session.scalars(
                    select(UserRecord).where(UserRecord.id.in_(user_ids))
                ).all()
            } if user_ids else {}
            actors = {
                admin.id: admin
                for admin in session.scalars(
                    select(AdminUserRecord).where(AdminUserRecord.id.in_(user_ids))
                ).all()
            } if user_ids else {}
            return [
                {
                    "id": log.id,
                    "action": log.action,
                    "actor": self._audit_admin(actors.get(log.actor_user_id)),
                    "target": (
                        self._audit_admin(actors.get(log.target_user_id))
                        if log.action == "admin_permission_updated"
                        else self._audit_user(target_users.get(log.target_user_id))
                    ),
                    "changes": json.loads(log.detail_json or "{}"),
                    "ip_address": log.ip_address,
                    "created_at": log.created_at.isoformat(),
                }
                for log in logs
            ]

    def update_admin_user(
        self,
        actor_admin_id: str,
        target_admin_id: str,
        *,
        can_manage_knowledge: bool,
        is_active: bool,
        ip_address: str | None,
    ) -> dict[str, object]:
        with self._session_factory.begin() as session:
            target = session.get(AdminUserRecord, target_admin_id)
            if target is None:
                raise ValueError("找不到指定后台管理员。")
            if actor_admin_id == target_admin_id and not is_active:
                raise ValueError("不能停用当前正在使用的后台管理员账号。")

            if target.is_active and not is_active:
                other_active_count = session.scalar(
                    select(func.count(AdminUserRecord.id)).where(
                        AdminUserRecord.is_active.is_(True),
                        AdminUserRecord.id != target_admin_id,
                    )
                ) or 0
                if other_active_count == 0:
                    raise ValueError("至少需要保留一个启用中的后台管理员账号。")

            previous = {
                "is_active": bool(target.is_active),
                "can_manage_knowledge": bool(target.can_manage_knowledge),
            }
            changed = previous != {
                "is_active": is_active,
                "can_manage_knowledge": can_manage_knowledge,
            }
            target.is_active = is_active
            target.can_manage_knowledge = can_manage_knowledge
            if changed:
                session.execute(
                    delete(AdminSessionRecord).where(
                        AdminSessionRecord.admin_id == target_admin_id
                    )
                )
                session.execute(
                    delete(AdminLoginChallengeRecord).where(
                        AdminLoginChallengeRecord.admin_id == target_admin_id
                    )
                )
                session.add(
                    AdminAuditRecord(
                        actor_user_id=actor_admin_id,
                        action="admin_permission_updated",
                        target_user_id=target_admin_id,
                        detail_json=json.dumps(
                            {
                                "before": previous,
                                "after": {
                                    "is_active": is_active,
                                    "can_manage_knowledge": can_manage_knowledge,
                                },
                            },
                            ensure_ascii=False,
                        ),
                        ip_address=(ip_address or "")[:64] or None,
                        created_at=_utc_now(),
                    )
                )
            session.flush()
            return self._admin_dict(target)

    @staticmethod
    def _user_dict(user: UserRecord) -> dict[str, object]:
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role or "member",
            "is_active": bool(user.is_active),
            "created_at": user.created_at.isoformat(),
            "last_login_at": (
                user.last_login_at.isoformat() if user.last_login_at else None
            ),
        }

    @staticmethod
    def _admin_dict(admin: AdminUserRecord) -> dict[str, object]:
        return {
            "id": admin.id,
            "email": admin.email,
            "display_name": admin.display_name,
            "is_active": bool(admin.is_active),
            "can_manage_knowledge": bool(admin.can_manage_knowledge),
            "created_at": admin.created_at.isoformat(),
            "last_login_at": (
                admin.last_login_at.isoformat() if admin.last_login_at else None
            ),
        }

    @staticmethod
    def _audit_user(user: UserRecord | None) -> dict[str, str] | None:
        if user is None:
            return None
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
        }

    @staticmethod
    def _audit_admin(admin: AdminUserRecord | None) -> dict[str, str] | None:
        if admin is None:
            return None
        return {
            "id": admin.id,
            "email": admin.email,
            "display_name": admin.display_name,
        }
