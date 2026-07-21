import os
import smtplib
import uuid
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")


def send_certificate_email(
    to_email: str,
    subject: str,
    html_body: str,
    attachment_bytes: bytes,
    attachment_name: str = "certificate.pdf",
    from_email: str = "",
    smtp_password: str = "",
    smtp_host: str = "",
    smtp_port: int = 465,
    text_body: str = "",
    smtp_secure: bool = True,
    sender_name: str = "",
):
    user = from_email or SMTP_USER
    password = smtp_password or SMTP_PASS
    from_header = sender_name or SMTP_FROM or user

    if not user or not password:
        print(f"[email mock] Would send to {to_email}: {subject} ({attachment_name})")
        return {"id": "mock", "status": "mock"}

    smtp_host = smtp_host or "smtp.gmail.com"

    msg = MIMEMultipart("alternative")
    msg["From"] = from_header
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@certifyauto>"
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    if attachment_bytes:
        mixed = MIMEMultipart("mixed")
        mixed.attach(msg)
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{attachment_name}"')
        mixed.attach(part)
        msg = mixed

    # Automatically deduce the correct security protocol based on port 
    # to prevent "Connection unexpectedly closed" errors
    if smtp_port == 465:
        smtp_secure = True
    elif smtp_port in (587, 25):
        smtp_secure = False

    if smtp_secure:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

    return {"id": "smtp", "status": "sent"}
