"""基于 SQLite 和 HttpOnly Cookie 的简单账号认证。"""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
import re
import secrets
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from hello_agent.database import (
    AgentSessionOwnerRecord,
    LoginSessionRecord,
    UserRecord,
    _create_session_factory,
)


PASSWORD_ITERATIONS = 600_000
LOGIN_SESSION_DAYS = 30
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    is_super_admin: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("请输入有效的邮箱地址。")
    return normalized


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("密码至少需要 8 个字符。")
    if len(password) > 128:
        raise ValueError("密码不能超过 128 个字符。")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


class SqliteAuthService:
    """管理用户、登录 Cookie 会话和 Agent 会话归属。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self._session_factory = _create_session_factory(
            str(self.database_path)
        )
        self._migrate_agent_roles()

    def register(self, email: str, password: str, display_name: str) -> AuthUser:
        normalized_email = _normalize_email(email)
        _validate_password(password)
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("昵称不能为空。")
        if len(normalized_name) > 50:
            raise ValueError("昵称不能超过 50 个字符。")

        try:
            with self._session_factory.begin() as session:
                record = UserRecord(
                    id=str(uuid4()),
                    email=normalized_email,
                    display_name=normalized_name,
                    password_hash=_hash_password(password),
                    role="member",
                    is_active=True,
                    created_at=_utc_now(),
                )
                session.add(record)
                session.flush()
        except IntegrityError as error:
            raise ValueError("该邮箱已经注册。") from error
        return self._to_user(record)

    def login(self, email: str, password: str) -> tuple[AuthUser, str]:
        normalized_email = _normalize_email(email)
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(UserRecord).where(UserRecord.email == normalized_email)
            )
            if record is None or not _verify_password(password, record.password_hash):
                raise ValueError("邮箱或密码不正确。")
            if not record.is_active:
                raise ValueError("账号已停用，请联系系统管理员。")
            record.last_login_at = _utc_now()
            user = self._to_user(record)
        return user, self.create_login_session(user.id)

    def create_login_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._session_factory.begin() as session:
            session.execute(
                delete(LoginSessionRecord).where(
                    LoginSessionRecord.expires_at <= _utc_now()
                )
            )
            session.add(
                LoginSessionRecord(
                    token_hash=self._token_hash(token),
                    user_id=user_id,
                    expires_at=_utc_now() + timedelta(days=LOGIN_SESSION_DAYS),
                )
            )
        return token

    def authenticate(self, token: str | None) -> AuthUser | None:
        if not token:
            return None
        with self._session_factory() as session:
            login_session = session.get(LoginSessionRecord, self._token_hash(token))
            if login_session is None or login_session.expires_at <= _utc_now():
                return None
            user = session.get(UserRecord, login_session.user_id)
            return (
                self._to_user(user)
                if user is not None and user.is_active
                else None
            )

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._session_factory.begin() as session:
            record = session.get(LoginSessionRecord, self._token_hash(token))
            if record is not None:
                session.delete(record)

    def claim_new_agent_session(self, user_id: str, session_id: str) -> None:
        with self._session_factory.begin() as session:
            owner = session.get(AgentSessionOwnerRecord, session_id)
            if owner is not None:
                if owner.user_id != user_id:
                    raise PermissionError("你无权访问这个 Agent 会话。")
                return
            session.add(
                AgentSessionOwnerRecord(session_id=session_id, user_id=user_id)
            )

    def owns_agent_session(self, user_id: str, session_id: str) -> bool:
        with self._session_factory() as session:
            owner = session.get(AgentSessionOwnerRecord, session_id)
            return owner is not None and owner.user_id == user_id

    def _migrate_agent_roles(self) -> None:
        """后台账号已独立，旧 Agent 管理员迁移为知识库管理员。"""
        with self._session_factory.begin() as session:
            session.execute(
                update(UserRecord)
                .where(UserRecord.role == "admin")
                .values(role="knowledge_manager")
            )

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _to_user(record: UserRecord) -> AuthUser:
        return AuthUser(
            id=record.id,
            email=record.email,
            display_name=record.display_name,
            role=record.role or "member",
            is_super_admin=bool(record.is_super_admin),
            is_active=bool(record.is_active),
        )
