"""独立于 Agent 用户的后台账号、TOTP 和会话认证。"""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import smtplib
import struct
import time
from email.message import EmailMessage
from urllib.parse import quote
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from hello_agent.auth import _hash_password, _normalize_email, _verify_password
from hello_agent.database import (
    AdminAuditRecord,
    AdminEmailCodeRecord,
    AdminLoginChallengeRecord,
    AdminRecoveryCodeRecord,
    AdminSessionRecord,
    AdminUserRecord,
    _create_session_factory,
)


ADMIN_SESSION_HOURS = 8
# 后台操作通常会跨越较长的管理流程；仍保留 8 小时绝对有效期作为上限。
ADMIN_IDLE_MINUTES = 180
CHALLENGE_MINUTES = 5
EMAIL_CODE_MINUTES = 5
EMAIL_RESEND_SECONDS = 60
MAX_ATTEMPTS = 5
LOCK_MINUTES = 15


@dataclass(frozen=True)
class AdminPrincipal:
    id: str
    email: str
    display_name: str
    role: str = "admin"
    mfa_rebind_required: bool = False
    can_manage_knowledge: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_admin_password(password: str) -> None:
    if len(password) < 12:
        raise ValueError("后台密码至少需要 12 个字符。")
    if len(password) > 128:
        raise ValueError("后台密码不能超过 128 个字符。")


def _totp(secret: str, counter: int) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


class SqliteAdminAuthService:
    """后台身份域：不接受 Agent 用户密码或 Agent 登录 Cookie。"""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()
        self._session_factory = _create_session_factory(str(self.database_path))
        configured_key = os.getenv("ADMIN_MFA_ENCRYPTION_KEY", "").strip()
        if configured_key:
            try:
                self._fernet = Fernet(configured_key.encode("ascii"))
            except (TypeError, ValueError) as error:
                raise RuntimeError("ADMIN_MFA_ENCRYPTION_KEY 格式不正确。") from error
        else:
            development_key = hashlib.sha256(
                f"hello-agent-development:{self.database_path}".encode()
            ).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(development_key))
        self._code_pepper = hashlib.sha256(
            (configured_key or f"development:{self.database_path}").encode()
        ).digest()

    def bootstrap(
        self,
        email: str,
        password: str,
        display_name: str = "系统管理员",
        can_manage_knowledge: bool = False,
    ) -> AdminPrincipal:
        normalized_email = _normalize_email(email)
        _validate_admin_password(password)
        normalized_name = display_name.strip()
        if not normalized_name or len(normalized_name) > 50:
            raise ValueError("管理员名称需要为 1 到 50 个字符。")
        try:
            with self._session_factory.begin() as session:
                record = AdminUserRecord(
                    id=str(uuid4()),
                    email=normalized_email,
                    display_name=normalized_name,
                    password_hash=_hash_password(password),
                    is_active=True,
                    mfa_enabled=False,
                    can_manage_knowledge=can_manage_knowledge,
                    password_must_change=True,
                    failed_attempts=0,
                    created_at=_utc_now(),
                )
                session.add(record)
                session.flush()
                return self._principal(record)
        except IntegrityError as error:
            raise ValueError("该后台管理员账号已经存在。") from error

    def has_admins(self) -> bool:
        with self._session_factory() as session:
            return session.scalar(select(AdminUserRecord.id).limit(1)) is not None

    def password_login(self, email: str, password: str) -> dict[str, object]:
        normalized_email = _normalize_email(email)
        now = _utc_now()
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(AdminUserRecord).where(AdminUserRecord.email == normalized_email)
            )
            if record is None or not _verify_password(password, record.password_hash):
                if record is not None:
                    record.failed_attempts += 1
                    if record.failed_attempts >= MAX_ATTEMPTS:
                        record.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                        record.failed_attempts = 0
                raise ValueError("后台账号或密码不正确。")
            if not record.is_active:
                raise ValueError("后台管理员账号已停用。")
            if record.locked_until and record.locked_until > now:
                raise ValueError("登录失败次数过多，请稍后再试。")
            record.failed_attempts = 0
            record.locked_until = None
            session.execute(
                delete(AdminLoginChallengeRecord).where(
                    AdminLoginChallengeRecord.admin_id == record.id
                )
            )
            token = secrets.token_urlsafe(32)
            session.add(
                AdminLoginChallengeRecord(
                    token_hash=_token_hash(token),
                    admin_id=record.id,
                    attempts=0,
                    expires_at=now + timedelta(minutes=CHALLENGE_MINUTES),
                )
            )
            return {
                "challenge_token": token,
                "requires_setup": not bool(record.mfa_enabled),
                "email_recovery_available": bool(
                    record.email_verified_at and record.email_recovery_enabled
                ),
                "expires_in": CHALLENGE_MINUTES * 60,
            }

    def start_mfa_setup(self, challenge_token: str) -> dict[str, str]:
        with self._session_factory.begin() as session:
            challenge, admin = self._challenge(session, challenge_token)
            if admin.mfa_enabled:
                raise ValueError("该管理员已完成双重认证绑定。")
            secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
            challenge.setup_secret_encrypted = self._encrypt(secret)
            label = quote(f"石山岭科技:{admin.email}")
            issuer = quote("石山岭科技 Agent Admin")
            return {
                "secret": secret,
                "otpauth_uri": (
                    f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
                    "&algorithm=SHA1&digits=6&period=30"
                ),
            }

    def activate_mfa(
        self, challenge_token: str, code: str, new_password: str
    ) -> tuple[AdminPrincipal, str, list[str]]:
        _validate_admin_password(new_password)
        with self._session_factory.begin() as session:
            challenge, admin = self._challenge(session, challenge_token)
            if not challenge.setup_secret_encrypted:
                raise ValueError("请先生成 Authenticator 绑定信息。")
            secret = self._decrypt(challenge.setup_secret_encrypted)
            counter = self._verify_totp(secret, code)
            if counter is None:
                self._failed_challenge(session, challenge)
                raise ValueError("动态验证码不正确或已过期。")
            admin.password_hash = _hash_password(new_password)
            admin.password_must_change = False
            admin.mfa_secret_encrypted = self._encrypt(secret)
            admin.mfa_enabled = True
            admin.last_totp_counter = counter
            admin.last_login_at = _utc_now()
            session.execute(
                delete(AdminRecoveryCodeRecord).where(
                    AdminRecoveryCodeRecord.admin_id == admin.id
                )
            )
            recovery_codes = [self._new_recovery_code() for _ in range(8)]
            session.add_all(
                AdminRecoveryCodeRecord(
                    admin_id=admin.id,
                    code_hash=_token_hash(code_value),
                )
                for code_value in recovery_codes
            )
            session.delete(challenge)
            token = self._create_session(session, admin.id)
            return self._principal(admin), token, recovery_codes

    def verify_mfa(
        self, challenge_token: str, code: str
    ) -> tuple[AdminPrincipal, str]:
        with self._session_factory.begin() as session:
            challenge, admin = self._challenge(session, challenge_token)
            if not admin.mfa_enabled or not admin.mfa_secret_encrypted:
                raise ValueError("请先绑定 Authenticator。")
            verified = False
            counter = self._verify_totp(self._decrypt(admin.mfa_secret_encrypted), code)
            if counter is not None and (
                admin.last_totp_counter is None or counter > admin.last_totp_counter
            ):
                admin.last_totp_counter = counter
                verified = True
            if not verified:
                normalized_code = code.strip().upper()
                recovery = session.scalar(
                    select(AdminRecoveryCodeRecord).where(
                        AdminRecoveryCodeRecord.admin_id == admin.id,
                        AdminRecoveryCodeRecord.code_hash == _token_hash(normalized_code),
                        AdminRecoveryCodeRecord.used_at.is_(None),
                    )
                )
                if recovery is not None:
                    recovery.used_at = _utc_now()
                    verified = True
            if not verified:
                self._failed_challenge(session, challenge)
                raise ValueError("动态验证码或恢复码不正确。")
            admin.last_login_at = _utc_now()
            session.delete(challenge)
            return self._principal(admin), self._create_session(session, admin.id)

    def send_email_enable_code(self, admin_id: str) -> str:
        with self._session_factory.begin() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if admin is None or not admin.is_active:
                raise ValueError("找不到管理员账号。")
            target_email = self._configured_recovery_email(admin)
            if not target_email:
                raise ValueError("请先填写用于双重认证的恢复邮箱。")
            admin.pending_recovery_email = target_email
            self._issue_email_code(
                session, admin, "email_enable", None, recipient=target_email
            )
            return self._mask_email(target_email)

    def send_email_enable_code_to(
        self, admin_id: str, recovery_email: str | None
    ) -> str:
        with self._session_factory.begin() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if admin is None or not admin.is_active:
                raise ValueError("找不到管理员账号。")
            target_email = self._resolve_recovery_email(admin, recovery_email)
            admin.pending_recovery_email = target_email
            self._issue_email_code(
                session, admin, "email_enable", None, recipient=target_email
            )
            return self._mask_email(target_email)

    def enable_email_recovery(self, admin_id: str, code: str) -> None:
        with self._session_factory.begin() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if admin is None:
                raise ValueError("找不到管理员账号。")
            self._consume_email_code(session, admin, "email_enable", code, None)
            target_email = (
                admin.pending_recovery_email or admin.recovery_email
            )
            if not target_email:
                raise ValueError("请先填写用于双重认证的恢复邮箱。")
            admin.recovery_email = target_email
            admin.pending_recovery_email = None
            admin.email_verified_at = _utc_now()
            admin.email_recovery_enabled = True
            self._audit(session, admin.id, "admin_email_recovery_enabled")

    def disable_email_recovery(self, admin_id: str) -> None:
        with self._session_factory.begin() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if admin is None:
                raise ValueError("找不到管理员账号。")
            admin.email_recovery_enabled = False
            admin.pending_recovery_email = None
            self._audit(session, admin.id, "admin_email_recovery_disabled")

    def send_login_email_code(self, challenge_token: str) -> str:
        challenge_hash = _token_hash(challenge_token)
        with self._session_factory.begin() as session:
            _challenge, admin = self._challenge(session, challenge_token)
            target_email = self._recovery_email(admin)
            if (
                not admin.email_verified_at
                or not admin.email_recovery_enabled
                or not target_email
            ):
                raise ValueError("该管理员尚未启用邮箱恢复认证。")
            self._issue_email_code(
                session, admin, "email_login", challenge_hash, recipient=target_email
            )
            return self._mask_email(target_email)

    def verify_login_email(
        self, challenge_token: str, code: str
    ) -> tuple[AdminPrincipal, str]:
        challenge_hash = _token_hash(challenge_token)
        with self._session_factory.begin() as session:
            challenge, admin = self._challenge(session, challenge_token)
            self._consume_email_code(
                session, admin, "email_login", code, challenge_hash
            )
            admin.mfa_rebind_required = False
            admin.last_login_at = _utc_now()
            session.delete(challenge)
            self._audit(session, admin.id, "admin_email_otp_login")
            return self._principal(admin), self._create_session(session, admin.id)

    def start_mfa_rebind(self, admin_id: str) -> dict[str, str]:
        with self._session_factory.begin() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if admin is None or not admin.mfa_rebind_required:
                raise ValueError("当前账号不需要重新绑定 Authenticator。")
            secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
            admin.pending_mfa_secret_encrypted = self._encrypt(secret)
            label = quote(f"石山岭科技:{admin.email}")
            issuer = quote("石山岭科技 Agent Admin")
            return {
                "secret": secret,
                "otpauth_uri": (
                    f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
                    "&algorithm=SHA1&digits=6&period=30"
                ),
            }

    def complete_mfa_rebind(
        self, admin_id: str, code: str, current_token: str
    ) -> tuple[AdminPrincipal, list[str]]:
        with self._session_factory.begin() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if (
                admin is None
                or not admin.mfa_rebind_required
                or not admin.pending_mfa_secret_encrypted
            ):
                raise ValueError("请先生成新的 Authenticator 绑定信息。")
            secret = self._decrypt(admin.pending_mfa_secret_encrypted)
            counter = self._verify_totp(secret, code)
            if counter is None:
                raise ValueError("动态验证码不正确或已过期。")
            admin.mfa_secret_encrypted = self._encrypt(secret)
            admin.pending_mfa_secret_encrypted = None
            admin.mfa_rebind_required = False
            admin.last_totp_counter = counter
            recovery_codes = self._replace_recovery_codes(session, admin.id)
            current_hash = _token_hash(current_token)
            session.execute(
                delete(AdminSessionRecord).where(
                    AdminSessionRecord.admin_id == admin.id,
                    AdminSessionRecord.token_hash != current_hash,
                )
            )
            self._audit(session, admin.id, "admin_authenticator_rebound")
            return self._principal(admin), recovery_codes

    def authenticate(self, token: str | None) -> AdminPrincipal | None:
        if not token:
            return None
        now = _utc_now()
        with self._session_factory.begin() as session:
            login = session.get(AdminSessionRecord, _token_hash(token))
            if (
                login is None
                or not login.mfa_verified
                or login.expires_at <= now
                or login.last_seen_at <= now - timedelta(minutes=ADMIN_IDLE_MINUTES)
            ):
                if login is not None:
                    session.delete(login)
                return None
            admin = session.get(AdminUserRecord, login.admin_id)
            if admin is None or not admin.is_active or not admin.mfa_enabled:
                session.delete(login)
                return None
            login.last_seen_at = now
            return self._principal(admin)

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._session_factory.begin() as session:
            login = session.get(AdminSessionRecord, _token_hash(token))
            if login is not None:
                session.delete(login)

    def security_status(self, admin_id: str) -> dict[str, object]:
        with self._session_factory() as session:
            admin = session.get(AdminUserRecord, admin_id)
            if admin is None:
                raise ValueError("找不到管理员账号。")
            recovery_codes = session.scalar(
                select(func.count(AdminRecoveryCodeRecord.id)).where(
                    AdminRecoveryCodeRecord.admin_id == admin_id,
                    AdminRecoveryCodeRecord.used_at.is_(None),
                )
            ) or 0
            active_sessions = session.scalar(
                select(func.count(AdminSessionRecord.token_hash)).where(
                    AdminSessionRecord.admin_id == admin_id,
                    AdminSessionRecord.expires_at > _utc_now(),
                )
            ) or 0
            return {
                "authenticator_enabled": bool(admin.mfa_enabled),
                "email_otp_available": bool(
                    os.getenv("SMTP_HOST", "").strip()
                    and os.getenv("SMTP_FROM", "").strip()
                    and os.getenv("SMTP_PASSWORD", "").strip()
                ),
                "email_verified": bool(admin.email_verified_at),
                "email_recovery_enabled": bool(admin.email_recovery_enabled),
                "masked_email": self._mask_email(self._recovery_email(admin)),
                "email": admin.email,
                "recovery_email": self._configured_recovery_email(admin),
                "recovery_codes_remaining": int(recovery_codes),
                "active_sessions": int(active_sessions),
                "idle_timeout_minutes": ADMIN_IDLE_MINUTES,
                "absolute_timeout_hours": ADMIN_SESSION_HOURS,
            }

    def revoke_other_sessions(self, admin_id: str, current_token: str) -> int:
        current_hash = _token_hash(current_token)
        with self._session_factory.begin() as session:
            sessions = session.scalars(
                select(AdminSessionRecord).where(
                    AdminSessionRecord.admin_id == admin_id,
                    AdminSessionRecord.token_hash != current_hash,
                )
            ).all()
            for login in sessions:
                session.delete(login)
            return len(sessions)

    def _challenge(self, session, token: str):
        challenge = session.get(AdminLoginChallengeRecord, _token_hash(token))
        if challenge is None or challenge.expires_at <= _utc_now():
            if challenge is not None:
                session.delete(challenge)
            raise ValueError("登录验证已过期，请重新输入密码。")
        admin = session.get(AdminUserRecord, challenge.admin_id)
        if admin is None or not admin.is_active:
            raise ValueError("后台管理员账号不可用。")
        return challenge, admin

    @staticmethod
    def _failed_challenge(session, challenge: AdminLoginChallengeRecord) -> None:
        challenge.attempts += 1
        if challenge.attempts >= MAX_ATTEMPTS:
            session.delete(challenge)

    def _create_session(self, session, admin_id: str) -> str:
        now = _utc_now()
        token = secrets.token_urlsafe(32)
        session.execute(
            delete(AdminSessionRecord).where(AdminSessionRecord.expires_at <= now)
        )
        session.add(
            AdminSessionRecord(
                token_hash=_token_hash(token),
                admin_id=admin_id,
                mfa_verified=True,
                last_seen_at=now,
                expires_at=now + timedelta(hours=ADMIN_SESSION_HOURS),
            )
        )
        return token

    def _issue_email_code(
        self, session, admin: AdminUserRecord, purpose: str,
        challenge_hash: str | None, recipient: str
    ) -> None:
        now = _utc_now()
        latest = session.scalar(
            select(AdminEmailCodeRecord)
            .where(
                AdminEmailCodeRecord.admin_id == admin.id,
                AdminEmailCodeRecord.purpose == purpose,
                AdminEmailCodeRecord.challenge_hash == challenge_hash,
            )
            .order_by(AdminEmailCodeRecord.sent_at.desc())
            .limit(1)
        )
        if latest and latest.sent_at > now - timedelta(seconds=EMAIL_RESEND_SECONDS):
            raise ValueError("验证码发送过于频繁，请 60 秒后再试。")
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._send_email(recipient, code, purpose)
        session.add(
            AdminEmailCodeRecord(
                id=str(uuid4()),
                admin_id=admin.id,
                purpose=purpose,
                challenge_hash=challenge_hash,
                code_hash=self._email_code_hash(admin.id, purpose, code),
                attempts=0,
                sent_at=now,
                expires_at=now + timedelta(minutes=EMAIL_CODE_MINUTES),
            )
        )

    def _consume_email_code(
        self, session, admin: AdminUserRecord, purpose: str, code: str,
        challenge_hash: str | None,
    ) -> None:
        record = session.scalar(
            select(AdminEmailCodeRecord)
            .where(
                AdminEmailCodeRecord.admin_id == admin.id,
                AdminEmailCodeRecord.purpose == purpose,
                AdminEmailCodeRecord.challenge_hash == challenge_hash,
                AdminEmailCodeRecord.used_at.is_(None),
            )
            .order_by(AdminEmailCodeRecord.sent_at.desc())
            .limit(1)
        )
        now = _utc_now()
        if record is None or record.expires_at <= now or record.attempts >= MAX_ATTEMPTS:
            raise ValueError("邮箱验证码不存在或已过期。")
        expected = self._email_code_hash(admin.id, purpose, code.strip())
        if not hmac.compare_digest(record.code_hash, expected):
            record.attempts += 1
            raise ValueError("邮箱验证码不正确。")
        record.used_at = now

    def _send_email(self, recipient: str, code: str, purpose: str) -> None:
        host = os.getenv("SMTP_HOST", "").strip()
        port = int(os.getenv("SMTP_PORT", "465"))
        username = os.getenv("SMTP_USER", "").strip()
        password = os.getenv("SMTP_PASSWORD", "").strip()
        sender = os.getenv("SMTP_FROM", "").strip()
        if not all((host, username, password, sender)):
            raise RuntimeError("SMTP 邮件服务尚未完整配置。")
        subject = "后台恢复邮箱验证" if purpose == "email_enable" else "后台登录验证码"
        message = EmailMessage()
        message["Subject"] = f"石山岭科技 Agent · {subject}"
        message["From"] = sender
        message["To"] = recipient
        message.set_content(
            f"你的验证码是：{code}\n\n验证码 5 分钟内有效，只能使用一次。"
            "如果不是你本人操作，请忽略此邮件并检查后台密码。"
        )
        purpose_title = "绑定邮箱验证" if purpose == "email_enable" else "邮箱验证码登录"
        icon_cid = "project-icon-v1-optimized"
        icon_html = (
            f'<img src="cid:{icon_cid}" alt="石山岭科技项目图标" '
            'style="width:56px;height:56px;border-radius:16px;display:block;box-shadow:0 10px 24px rgba(79,70,229,.22);" />'
            if self._brand_icon_bytes() is not None
            else '<div style="width:56px;height:56px;border-radius:16px;display:grid;place-items:center;background:linear-gradient(135deg,#2563eb 0%,#7c3aed 100%);color:#ffffff;font-size:28px;font-weight:800;">S</div>'
        )
        message.add_alternative(
            f"""
<!DOCTYPE html>
<html lang="zh-CN">
  <body style="margin:0;padding:24px;background:#f4f7fb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1f2937;">
    <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:20px;overflow:hidden;border:1px solid #e5e7eb;box-shadow:0 18px 40px rgba(15,23,42,.08);">
      <div style="padding:22px 24px;background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);color:#ffffff;">
        <div style="margin-bottom:14px;">{icon_html}</div>
        <div style="font-size:13px;letter-spacing:.12em;opacity:.82;">SHISHANLING AGENT ADMIN</div>
        <div style="margin-top:10px;font-size:24px;font-weight:700;">{subject}</div>
        <div style="margin-top:6px;font-size:14px;opacity:.9;">使用邮箱完成后台二次验证</div>
      </div>
      <div style="padding:28px 24px 26px;">
        <div style="font-size:15px;line-height:1.8;color:#4b5563;">
          你正在进行 <strong style="color:#111827;">{purpose_title}</strong>。请输入下面的 6 位验证码：
        </div>
        <div style="margin:22px 0;padding:18px 20px;border-radius:16px;background:#f8faff;border:1px solid #dbe7ff;text-align:center;">
          <div style="font-size:34px;line-height:1;font-weight:800;letter-spacing:.35em;color:#312e81;text-indent:.35em;">{code}</div>
        </div>
        <div style="display:grid;gap:10px;font-size:14px;line-height:1.7;color:#6b7280;">
          <div>验证码 5 分钟内有效，只能使用一次。</div>
          <div>同一功能发送过于频繁时，系统会限制 60 秒内重复发送。</div>
          <div>如果这不是你本人操作，请忽略此邮件并尽快修改后台密码。</div>
        </div>
      </div>
    </div>
  </body>
</html>
            """.strip(),
            subtype="html",
        )
        icon_bytes = self._brand_icon_bytes()
        if icon_bytes is not None:
            message.get_payload()[-1].add_related(
                icon_bytes,
                maintype="image",
                subtype="png",
                cid=f"<{icon_cid}>",
                filename="project-icon-v1-optimized.png",
            )
        use_ssl = os.getenv("SMTP_USE_SSL", "true").lower() == "true"
        try:
            if use_ssl:
                with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
                    smtp.login(username, password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=15) as smtp:
                    smtp.starttls()
                    smtp.login(username, password)
                    smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            raise RuntimeError("验证码邮件发送失败，请检查 SMTP 配置。") from error

    def _email_code_hash(self, admin_id: str, purpose: str, code: str) -> str:
        return hmac.new(
            self._code_pepper,
            f"{admin_id}:{purpose}:{code}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _replace_recovery_codes(self, session, admin_id: str) -> list[str]:
        session.execute(
            delete(AdminRecoveryCodeRecord).where(
                AdminRecoveryCodeRecord.admin_id == admin_id
            )
        )
        codes = [self._new_recovery_code() for _ in range(8)]
        session.add_all(
            AdminRecoveryCodeRecord(
                admin_id=admin_id, code_hash=_token_hash(code_value)
            )
            for code_value in codes
        )
        return codes

    @staticmethod
    def _mask_email(email: str) -> str:
        if not email:
            return "未设置"
        local, domain = email.split("@", 1)
        visible = local[:2] if len(local) > 2 else local[:1]
        return f"{visible}{'*' * max(3, len(local) - len(visible))}@{domain}"

    @staticmethod
    def _configured_recovery_email(admin: AdminUserRecord) -> str | None:
        return (admin.recovery_email or "").strip() or None

    def _resolve_recovery_email(
        self, admin: AdminUserRecord, recovery_email: str | None
    ) -> str:
        if recovery_email and recovery_email.strip():
            return _normalize_email(recovery_email)
        configured = self._configured_recovery_email(admin)
        if configured:
            return configured
        raise ValueError("请先填写用于双重认证的恢复邮箱。")

    @staticmethod
    def _recovery_email(admin: AdminUserRecord) -> str:
        configured = (admin.recovery_email or "").strip()
        if configured:
            return configured
        if admin.email_verified_at:
            return admin.email
        return ""

    @staticmethod
    def _audit(session, admin_id: str, action: str) -> None:
        session.add(
            AdminAuditRecord(
                actor_user_id=admin_id,
                action=action,
                target_user_id=None,
                detail_json="{}",
                created_at=_utc_now(),
            )
        )

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

    @staticmethod
    def _verify_totp(secret: str, code: str) -> int | None:
        normalized = code.strip()
        if len(normalized) != 6 or not normalized.isdigit():
            return None
        current = int(time.time() // 30)
        for counter in (current - 1, current, current + 1):
            if hmac.compare_digest(_totp(secret, counter), normalized):
                return counter
        return None

    @staticmethod
    def _new_recovery_code() -> str:
        raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
        return f"{raw[:8]}-{raw[8:16]}"

    @staticmethod
    def _brand_icon_bytes() -> bytes | None:
        icon_path = Path(__file__).resolve().with_name(
            "project-icon-v1-optimized.png"
        )
        if not icon_path.exists():
            return None
        return icon_path.read_bytes()

    @staticmethod
    def _principal(record: AdminUserRecord) -> AdminPrincipal:
        return AdminPrincipal(
            id=record.id,
            email=record.email,
            display_name=record.display_name,
            mfa_rebind_required=bool(record.mfa_rebind_required),
            can_manage_knowledge=bool(record.can_manage_knowledge),
        )
