# Email delivery for password resets and security alerts. Uses stdlib
# smtplib, run off the event loop via asyncio.to_thread since it's a
# blocking socket client.
import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD and settings.SMTP_FROM_EMAIL)


def _send_sync(to_email: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.SMTP_FROM_EMAIL
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.send_message(message)


async def _send(to_email: str, subject: str, body: str, log_label: str) -> bool:
    if not is_configured():
        return False
    try:
        await asyncio.wait_for(asyncio.to_thread(_send_sync, to_email, subject, body), timeout=15.0)
        return True
    except Exception as exc:
        logger.error("Failed to send %s email to %s: %s", log_label, to_email, exc)
        return False


async def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    """Returns False if SMTP isn't configured; caller falls back to a console print."""
    body = (
        f"We received a request to reset your IntervYou password.\n\n"
        f"Reset it here: {reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email."
    )
    return await _send(to_email, "Reset your IntervYou password", body, "password-reset")


async def send_security_alert_email(to_email: str, message: str) -> bool:
    """Sent by token_service when a refresh-token reuse attack is detected."""
    if not is_configured():
        logger.warning("[DEV] Security alert for %s (SMTP not configured): %s", to_email, message)
        return False
    return await _send(to_email, "Security alert: unusual activity on your account", message, "security-alert")
