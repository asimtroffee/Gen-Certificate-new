"""
Async email sender using aiosmtplib for high-concurrency bulk email dispatch.

Provides:
- send_single_email_async(): Send one email without blocking the event loop.
- send_bulk_emails_async():  Send hundreds of emails concurrently in controlled
                             batches (default 50 concurrent SMTP connections).
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Optional

import aiosmtplib
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

# ── Default SMTP settings from environment ──────────────────────────────────
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")

# ── Global concurrency control ──────────────────────────────────────────────
# Limits how many SMTP connections can be open simultaneously.
_smtp_semaphore = asyncio.Semaphore(50)


@dataclass
class EmailJob:
    """Describes a single email to send."""
    to_email: str
    subject: str
    html_body: str
    text_body: str = ""
    attachment_bytes: Optional[bytes] = None
    attachment_name: str = "attachment"
    from_email: str = ""
    smtp_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_secure: bool = True
    sender_name: str = ""


def _build_message(job: EmailJob) -> MIMEMultipart:
    """Build a MIMEMultipart message from an EmailJob."""
    user = job.from_email or SMTP_USER
    from_header = job.sender_name or SMTP_FROM or user

    msg = MIMEMultipart("alternative")
    msg["From"] = from_header
    msg["To"] = job.to_email
    msg["Subject"] = job.subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@certifyauto>"
    msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    if job.text_body:
        msg.attach(MIMEText(job.text_body, "plain"))
    msg.attach(MIMEText(job.html_body, "html"))

    if job.attachment_bytes:
        mixed = MIMEMultipart("mixed")
        mixed.attach(msg)
        part = MIMEBase("application", "octet-stream")
        part.set_payload(job.attachment_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{job.attachment_name}"',
        )
        mixed.attach(part)
        return mixed

    return msg


async def send_single_email_async(job: EmailJob) -> dict:
    """
    Send a single email asynchronously via aiosmtplib.
    Respects the global semaphore to avoid overwhelming the SMTP server.
    """
    user = job.from_email or SMTP_USER
    password = job.smtp_password or SMTP_PASS
    host = job.smtp_host or "smtp.gmail.com"
    port = job.smtp_port

    if not user or not password:
        print(f"[async_email mock] Would send to {job.to_email}: {job.subject}")
        return {"id": "mock", "status": "mock", "to": job.to_email}

    msg = _build_message(job)

    # Auto-detect TLS mode from port
    use_tls = port == 465
    start_tls = port in (587, 25)

    async with _smtp_semaphore:
        try:
            if use_tls:
                await aiosmtplib.send(
                    msg,
                    hostname=host,
                    port=port,
                    username=user,
                    password=password,
                    use_tls=True,
                )
            else:
                await aiosmtplib.send(
                    msg,
                    hostname=host,
                    port=port,
                    username=user,
                    password=password,
                    start_tls=start_tls,
                )
        except Exception as e:
            print(f"[async_email] Failed to send to {job.to_email}: {e}")
            raise

    return {"id": "smtp", "status": "sent", "to": job.to_email}


async def send_bulk_emails_async(
    jobs: list[EmailJob],
    batch_size: int = 50,
    on_success: callable = None,
    on_error: callable = None,
) -> dict:
    """
    Send a list of emails concurrently in controlled batches.

    Args:
        jobs:       List of EmailJob objects to send.
        batch_size: How many emails to send concurrently per batch (default 50).
        on_success: Optional async callback(job, result) called after each success.
        on_error:   Optional async callback(job, exception) called after each failure.

    Returns:
        Summary dict with sent/failed counts.
    """
    sent = 0
    failed = 0
    total = len(jobs)

    for batch_start in range(0, total, batch_size):
        batch = jobs[batch_start : batch_start + batch_size]

        async def _send_one(job: EmailJob):
            nonlocal sent, failed
            try:
                result = await send_single_email_async(job)
                sent += 1
                if on_success:
                    await on_success(job, result)
            except Exception as e:
                failed += 1
                print(f"[bulk_email] Error sending to {job.to_email}: {e}")
                if on_error:
                    await on_error(job, e)

        await asyncio.gather(*[_send_one(j) for j in batch])

        # Small delay between batches to be kind to the SMTP server
        if batch_start + batch_size < total:
            await asyncio.sleep(0.5)

    print(f"[bulk_email] Complete: {sent} sent, {failed} failed out of {total}")
    return {"sent": sent, "failed": failed, "total": total}
