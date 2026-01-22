from __future__ import annotations

import json
import logging
import mimetypes
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Sequence

from spider.config import get_env_bool, get_env_int, get_env_list, get_env_str

logger = logging.getLogger(__name__)

_DEFAULT_SUBJECT = "Weibo cookie invalid"
_DEFAULT_COOLDOWN_SECONDS = 900
_DEFAULT_SMTP_TIMEOUT = 10
_DEFAULT_QR_PATH = Path(__file__).resolve().parent / "weibo_login_qrcode.png"
_STATE_PATH = Path(__file__).resolve().parent / "notify_email_state.json"

_NOTIFY_LOCK = threading.Lock()
_LAST_NOTIFY_AT: dict[str, float] = {}


def notify_cookie_invalid(label: str, reason: str) -> None:
    """Send notification email when a cookie is marked invalid."""
    if not get_env_bool("WEIBO_EMAIL_NOTIFY_ENABLED", False):
        return

    now = time.time()
    cooldown = get_env_int("WEIBO_EMAIL_NOTIFY_COOLDOWN", _DEFAULT_COOLDOWN_SECONDS) or _DEFAULT_COOLDOWN_SECONDS
    qr_path = _resolve_qr_path()

    with _NOTIFY_LOCK:
        qr_signature = _get_qr_signature(qr_path)
        if not qr_signature:
            logger.warning("Email notify skipped: QR code file missing or unreadable: %s", qr_path)
            return

        state = _load_notify_state()
        signatures = state.setdefault("qr_signatures", {})
        last_signature = signatures.get(str(qr_path))
        if last_signature == qr_signature:
            logger.info("Email notify skipped: QR code not updated yet: %s", qr_path)
            return

        last = _LAST_NOTIFY_AT.get(label, 0.0)
        if last and (now - last) < cooldown:
            return
        _LAST_NOTIFY_AT[label] = now

        ok = _send_cookie_invalid_email(label, reason, qr_path=qr_path)
        if not ok:
            if _LAST_NOTIFY_AT.get(label) == now:
                _LAST_NOTIFY_AT.pop(label, None)
            return

        signatures[str(qr_path)] = qr_signature
        _save_notify_state(state)


def _send_cookie_invalid_email(label: str, reason: str, *, qr_path: Optional[Path] = None) -> bool:
    user = (get_env_str("WEIBO_EMAIL_USER") or "").strip()
    password = (get_env_str("WEIBO_EMAIL_AUTH_CODE") or "").strip()
    to_list = get_env_list("WEIBO_EMAIL_TO")
    if not user or not password or not to_list:
        logger.warning("Email notify skipped: missing WEIBO_EMAIL_USER/AUTH_CODE/TO.")
        return False

    host = get_env_str("WEIBO_EMAIL_SMTP_HOST", "smtp.163.com") or "smtp.163.com"
    port = get_env_int("WEIBO_EMAIL_SMTP_PORT", 465) or 465
    use_ssl = get_env_bool("WEIBO_EMAIL_SMTP_SSL", True)
    use_starttls = get_env_bool("WEIBO_EMAIL_SMTP_STARTTLS", False)
    timeout = get_env_int("WEIBO_EMAIL_SMTP_TIMEOUT", _DEFAULT_SMTP_TIMEOUT) or _DEFAULT_SMTP_TIMEOUT

    sender = (get_env_str("WEIBO_EMAIL_FROM", user) or user).strip()
    subject = get_env_str("WEIBO_EMAIL_SUBJECT", _DEFAULT_SUBJECT) or _DEFAULT_SUBJECT

    qr_path = qr_path or _resolve_qr_path()

    body_lines = [
        "Weibo cookie marked invalid.",
        f"Cookie label: {label}",
        f"Reason: {reason or 'unknown'}",
        f"Time: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if qr_path.exists():
        body_lines.append(f"QR path: {qr_path}")
    else:
        body_lines.append("QR path: missing")
        logger.warning("QR code file missing: %s", qr_path)

    message = _build_message(sender, to_list, subject, "\n".join(body_lines), qr_path if qr_path.exists() else None)

    try:
        _send_message(message, host, port, user, password, use_ssl, use_starttls, timeout)
    except Exception as exc:
        logger.warning("Email notify failed: %s", exc)
        return False

    logger.info("Email notify sent to %s", ", ".join(to_list))
    return True


def _resolve_qr_path() -> Path:
    qr_path_raw = (get_env_str("WEIBO_EMAIL_QR_PATH") or "").strip()
    return Path(qr_path_raw) if qr_path_raw else _DEFAULT_QR_PATH


def _get_qr_signature(qr_path: Path) -> Optional[str]:
    try:
        stat = qr_path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _load_notify_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_notify_state(state: dict) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("Failed to persist notify email state: %s", _STATE_PATH)


def _build_message(
    sender: str,
    recipients: Sequence[str],
    subject: str,
    body: str,
    attachment: Optional[Path],
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    if attachment:
        data = attachment.read_bytes()
        mime_type, _ = mimetypes.guess_type(str(attachment))
        if not mime_type:
            mime_type = "application/octet-stream"
        maintype, subtype = mime_type.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment.name)

    return msg


def _send_message(
    message: EmailMessage,
    host: str,
    port: int,
    user: str,
    password: str,
    use_ssl: bool,
    use_starttls: bool,
    timeout: int,
) -> None:
    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if use_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(message)
