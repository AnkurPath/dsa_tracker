"""Outbound email via SMTP (stdlib)."""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("SMTP_FROM"))


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    return email


def send_email(*, to: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    """Send an email. Raises ValueError if SMTP is not configured or send fails."""
    host = os.getenv("SMTP_HOST", "").strip()
    from_addr = os.getenv("SMTP_FROM", "").strip()
    if not host or not from_addr:
        raise ValueError(
            "Email is not configured. Set SMTP_HOST and SMTP_FROM in the environment."
        )

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "").strip() or None
    password = os.getenv("SMTP_PASSWORD", "").strip() or None
    use_tls = os.getenv("SMTP_TLS", "1").strip() not in {"0", "false", "False"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    except OSError as exc:
        raise ValueError(f"Could not send email: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise ValueError(f"Could not send email: {exc}") from exc
