# Email delivery for the password-reset flow. Uses stdlib smtplib (no new
# dependency) run off the event loop via asyncio.to_thread, since smtplib
# is a blocking socket client.
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


async def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    """
    Returns True if a real email was sent, False if SMTP isn't configured
    (caller should fall back to the dev-mode console print in that case).
    Raises nothing on delivery failure — a bad SMTP config must not break
    the password-reset flow's anti-enumeration guarantee (always 200
    regardless of whether the email exists or whether delivery worked).
    """
    if not is_configured():
        return False

    subject = "Reset your IntervYou password"
    body = (
        f"We received a request to reset your IntervYou password.\n\n"
        f"Reset it here: {reset_link}\n\n"
        f"If you didn't request this, you can safely ignore this email."
    )
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_send_sync, to_email, subject, body),
            timeout=15.0,
        )
        return True
    except Exception as exc:
        logger.error("Failed to send password-reset email to %s: %s", to_email, exc)
        return False
