"""Admin dashboard API routes."""

import asyncio
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import state
from .supabase_init import get_client
from .google_sheets import (
    get_new_responses,
    mark_processed,
    download_form_file,
    get_file_name,
)
from .form_watcher import _process_submission as _watcher_process
from .async_email import EmailJob, send_single_email_async, send_bulk_emails_async

router = APIRouter(prefix="/api/admin")


def _get_smtp_config(client) -> dict:
    """Gather SMTP configuration from Supabase config table + .env fallbacks.
    
    Returns a dict suitable for **kwargs into EmailJob constructor.
    """
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_from = os.environ.get("SMTP_FROM", "")

    try:
        cfgs = client.table("config").select("*").execute()
        for item in cfgs.data or []:
            k, v = item.get("key", ""), item.get("value", "")
            if k == "sender_email" and v:
                smtp_user = v
            elif k == "sender_password" and v:
                smtp_pass = v
            elif k == "smtp_host" and v:
                smtp_host = v
            elif k == "smtp_port" and v:
                smtp_port = int(v)
            elif k == "sender_name" and v:
                smtp_from = v
    except Exception:
        pass

    return {
        "from_email": smtp_user,
        "smtp_password": smtp_pass,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "sender_name": smtp_from,
    }


def _get_public_url(client) -> str:
    """Retrieve public URL from Supabase config or .env."""
    base = os.environ.get("PUBLIC_URL", "http://localhost:8000")
    try:
        r = client.table("config").select("value").eq("key", "public_url").limit(1).execute()
        if r.data and r.data[0].get("value"):
            base = r.data[0]["value"].rstrip("/")
    except Exception:
        pass
    return base


async def _handle_bounce(job, exc):
    error_msg = str(exc)
    token = job.metadata.get("token")
    if token:
        client = get_client()
        if client:
            try:
                client.table("teacher_links").update({
                    "email_bounced": True,
                    "bounce_error": error_msg[:500]
                }).eq("token", token).execute()
            except Exception as e:
                print(f"[handle_bounce] Supabase update error: {e}")


async def _background_bulk_email(jobs: list, event_id: str):
    """Background task: sends all emails and logs results."""
    try:
        result = await send_bulk_emails_async(jobs, batch_size=50, on_error=_handle_bounce)
        print(f"[bulk_email] Event {event_id}: {result['sent']} sent, {result['failed']} failed")
    except Exception as e:
        print(f"[bulk_email] Event {event_id}: Fatal error: {e}")


@router.get("/stats")
async def get_stats():
    """Get dashboard KPI stats."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    # Aggregate events
    events_res = client.table("events").select("id, config_json").execute()
    total_events = len(events_res.data) if events_res.data else 0

    certificates_generated = 0
    for e in (events_res.data or []):
        config = e.get("config_json") or {}
        if isinstance(config, str):
            try:
                import json
                config = json.loads(config)
            except Exception:
                config = {}
        if isinstance(config, dict):
            certificates_generated += config.get("generated_count", 0)

    # Aggregate links (graceful — table may not exist yet)
    magic_links = 0
    submissions = 0
    try:
        links = client.table("teacher_links").select("id, used").execute()
        magic_links = len(links.data) if links.data else 0
        submissions = sum(1 for link in (links.data or []) if link.get("used"))
    except Exception as e:
        print(f"[admin/stats] teacher_links not found (run schema.sql?): {e}")

    return {
        "total_events": total_events,
        "magic_links_sent": magic_links,
        "submissions": submissions,
        "certificates_generated": certificates_generated
    }



@router.get("/teachers")
async def list_teachers():
    """List all teacher links with student counts and event info."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    # Get teacher links (magic links sent to teachers)
    links = client.table("teacher_links").select("*, events(name)").order("created_at", desc=True).execute()
    all_links = links.data or []

    # Get per-event generated counts from certificates table
    certs = client.table("certificates").select("batch_id, name").execute()
    batch_counts = {}
    for c in certs.data or []:
        bid = c.get("batch_id", "")
        batch_counts[bid] = batch_counts.get(bid, 0) + 1

    result = []
    for link in all_links:
        event_name = ""
        event_data = link.get("events")
        if event_data:
            event_name = event_data.get("name", "")

        result.append({
            "id": link.get("id"),
            "event_id": link.get("event_id", ""),
            "event_name": event_name,
            "teacher_name": link.get("teacher_name", ""),
            "teacher_email": link.get("teacher_email", ""),
            "student_count": batch_counts.get(link.get("id"), 0),
            "status": "used" if link.get("used") else "pending",
            "created_at": link.get("created_at"),
            "email_bounced": link.get("email_bounced", False),
            "bounce_error": link.get("bounce_error", ""),
        })

    return {"teachers": result, "total": len(result)}


@router.get("/teachers/{batch_id}")
async def teacher_detail(batch_id: str):
    """Get all students for a specific batch."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    students = (
        client.table("certificates")
        .select("*")
        .eq("batch_id", batch_id)
        .order("name")
        .execute()
    )

    teacher_info = {}

    student_list = []
    for s in students.data or []:
        storage_path = s.get("storage_path", "")
        download_url = ""
        if storage_path:
            download_url = client.storage.from_("certificates").get_public_url(storage_path)
        student_list.append({
            "id": s.get("id"),
            "name": s.get("name", ""),
            "email": s.get("email", ""),
            "download_url": download_url,
            "format": s.get("format", "png"),
            "sent": s.get("sent", False),
        })

    return {
        "batch_id": batch_id,
        "teacher_name": teacher_info.get("teacher_name", "Unknown"),
        "teacher_email": teacher_info.get("teacher_email", ""),
        "students": student_list,
        "total": len(student_list),
    }


@router.get("/events/{event_id}/teachers")
async def event_teachers(event_id: str):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    links = client.table("teacher_links").select("*").eq("event_id", event_id).order("created_at", desc=True).execute()
    all_links = links.data or []

    # Get per-event generated counts from certificates table
    certs = client.table("certificates").select("batch_id, name").execute()
    batch_counts = {}
    for c in certs.data or []:
        bid = c.get("batch_id", "")
        batch_counts[bid] = batch_counts.get(bid, 0) + 1

    result = []
    for link in all_links:
        result.append({
            "id": link.get("id"),
            "event_id": link.get("event_id", ""),
            "teacher_name": link.get("teacher_name", ""),
            "teacher_email": link.get("teacher_email", ""),
            "student_count": batch_counts.get(link.get("id"), 0),
            "status": "used" if link.get("used") else "pending",
            "created_at": link.get("created_at"),
            "email_bounced": link.get("email_bounced", False),
            "bounce_error": link.get("bounce_error", ""),
        })

    return {"teachers": result, "total": len(result)}


from fastapi import UploadFile, File
import openpyxl
from io import BytesIO


def _parse_excel_sync(content: bytes):
    """CPU-bound Excel parsing — runs in a thread via asyncio.to_thread."""
    wb = openpyxl.load_workbook(filename=BytesIO(content), data_only=True)
    sheet = wb.active
    teachers = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        name = str(row[0] or "").strip()
        email = str(row[1] or "").strip()
        if name and email and "@" in email:
            teachers.append({"name": name, "email": email})
    return teachers


@router.post("/events/{event_id}/teachers/upload")
async def upload_event_teachers(event_id: str, file: UploadFile = File(...)):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    event_res = client.table("events").select("*").eq("id", event_id).limit(1).execute()
    if not event_res.data:
        raise HTTPException(404, "Event not found")

    content = await file.read()

    # Offload CPU-bound Excel parsing to a thread so it doesn't block the event loop
    try:
        teachers_to_add = await asyncio.to_thread(_parse_excel_sync, content)
    except Exception as e:
        raise HTTPException(400, f"Invalid Excel file: {e}")

    if not teachers_to_add:
        raise HTTPException(400, "No valid teachers found in file. Ensure columns are Name, Email.")

    added = 0
    import uuid
    for t in teachers_to_add:
        # Check if already exists
        existing = client.table("teacher_links").select("id").eq("event_id", event_id).eq("teacher_email", t["email"]).execute()
        if not existing.data:
            client.table("teacher_links").insert({
                "token": str(uuid.uuid4()),
                "event_id": event_id,
                "teacher_name": t["name"],
                "teacher_email": t["email"]
            }).execute()
            added += 1

    return {"ok": True, "added": added, "total_processed": len(teachers_to_add)}


class CreateLinkRequest(BaseModel):
    event_id: str = Field(..., min_length=1)
    teacher_email: str = Field(..., min_length=1)
    teacher_name: str = ""

@router.post("/link")
async def create_magic_link(req: CreateLinkRequest):
    """Create a new magic link for a teacher and email it."""
    import uuid
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    event_res = client.table("events").select("*").eq("id", req.event_id).limit(1).execute()
    if not event_res.data:
        raise HTTPException(404, "Event not found")
    event = event_res.data[0]
    event_name = event.get("name", "Event")
    event_type = event.get("event_type", "student")

    token_val = str(uuid.uuid4())
    try:
        client.table("teacher_links").insert({
            "token": token_val,
            "event_id": req.event_id,
            "teacher_name": req.teacher_name or "Teacher",
            "teacher_email": req.teacher_email,
        }).execute()
    except Exception as e:
        raise HTTPException(500, f"Failed to create link: {e}")

    # Gather SMTP config
    smtp_cfg = _get_smtp_config(client)
    raw_sender = smtp_cfg.get("sender_name", "")
    email_only = raw_sender.split("<")[1].split(">")[0] if "<" in raw_sender else raw_sender
    smtp_cfg["sender_name"] = f"{event_name} <{email_only}>"

    base_url = _get_public_url(client)
    html_page = "teacher-cert.html" if event_type == "teacher" else "teacher.html"
    link_url = f"{base_url}/{html_page}?t={token_val}"
    teacher_display = req.teacher_name or "Teacher"

    html_msg = f"""
    <p>Dear Teachers,</p>
    <p>Thank you for participating in the <strong>{event_name}</strong>! We truly appreciate your time, engagement, and dedication to continuous learning. Your active involvement made the event a resounding success.</p>
    
    <p><strong>How to Download Your Certificate</strong></p>
    <p>Your certificate of completion is now ready. You can easily generate and download it directly from the portal by following these steps:</p>
    <ol>
        <li><strong>Visit the Portal:</strong> Go to <a href="{link_url}">Your Personal Portal</a>.</li>
        <li><strong>Enter Your Details:</strong> Type in your Full Name and the School you represent.</li>
        <li><strong>Download:</strong> Click the Download button, and your certificate will automatically save to your device's downloads folder.</li>
    </ol>
    
    <p><strong>Note:</strong> Please double-check the spelling of your name and school before submitting to ensure your certificate generates accurately. If you run into any issues accessing the portal or downloading your file, feel free to reply directly to this email or contact us at <a href="mailto:asimiyad.troffee@gmail.com">asimiyad.troffee@gmail.com</a>.</p>
    
    <p>Thank you once again for your commitment to education. We look forward to seeing you at future workshops!</p>
    <p>Warm regards,<br>The Event Team</p>
    """

    text_body = f"Dear Teachers,\n\nThank you for participating in the {event_name}! We truly appreciate your time, engagement, and dedication to continuous learning. Your active involvement made the event a resounding success.\n\nHow to Download Your Certificate\nYour certificate of completion is now ready. You can easily generate and download it directly from the portal by following these steps:\n\n1. Visit the Portal: Go to {link_url}\n2. Enter Your Details: Type in your Full Name and the School you represent.\n3. Download: Click the Download button, and your certificate will automatically save to your device's downloads folder.\n\nNote: Please double-check the spelling of your name and school before submitting to ensure your certificate generates accurately. If you run into any issues accessing the portal or downloading your file, feel free to reply directly to this email or contact us at asimiyad.troffee@gmail.com.\n\nThank you once again for your commitment to education. We look forward to seeing you at future workshops!\n\nWarm regards,\nThe Event Team"

    job = EmailJob(
        to_email=req.teacher_email,
        subject=f"Thank You for Attending {event_name} | Download Your Certificate",
        html_body=html_msg,
        text_body=text_body,
        **smtp_cfg,
    )

    try:
        await send_single_email_async(job)
    except Exception as e:
        try:
            client.table("teacher_links").update({
                "email_bounced": True,
                "bounce_error": str(e)[:500]
            }).eq("token", token_val).execute()
        except Exception:
            pass
        raise HTTPException(500, f"Link created but email failed: {e}")

    return {"ok": True, "token": token_val, "message": f"Link sent to {req.teacher_email}"}


@router.post("/resend-link")
async def resend_link(req: CreateLinkRequest):
    """Resend an invitation link for a teacher. Creates a new link if none exists."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    # Look for existing unused link
    existing = client.table("teacher_links").select("*").eq("event_id", req.event_id).eq("teacher_email", req.teacher_email).order("created_at", desc=True).limit(1).execute()
    if existing.data:
        link = existing.data[0]
        token_val = link["token"]
        teacher_name = link.get("teacher_name", req.teacher_name or "Teacher")
    else:
        # Create new
        import uuid
        token_val = str(uuid.uuid4())
        teacher_name = req.teacher_name or "Teacher"
        try:
            client.table("teacher_links").insert({
                "token": token_val,
                "event_id": req.event_id,
                "teacher_name": teacher_name,
                "teacher_email": req.teacher_email,
            }).execute()
        except Exception as e:
            raise HTTPException(500, f"Failed to create link: {e}")

    event_res = client.table("events").select("name, event_type").eq("id", req.event_id).limit(1).execute()
    if not event_res.data:
        raise HTTPException(404, "Event not found")
    event_name = event_res.data[0]["name"]
    event_type = event_res.data[0].get("event_type", "student")

    smtp_cfg = _get_smtp_config(client)
    raw_sender = smtp_cfg.get("sender_name", "")
    email_only = raw_sender.split("<")[1].split(">")[0] if "<" in raw_sender else raw_sender
    smtp_cfg["sender_name"] = f"{event_name} <{email_only}>"

    base_url = _get_public_url(client)
    html_page = "teacher-cert.html" if event_type == "teacher" else "teacher.html"
    link_url = f"{base_url}/{html_page}?t={token_val}"

    html_msg = f"""
    <p>Dear Teachers,</p>
    <p>Thank you for participating in the <strong>{event_name}</strong>! We truly appreciate your time, engagement, and dedication to continuous learning. Your active involvement made the event a resounding success.</p>
    
    <p><strong>How to Download Your Certificate</strong></p>
    <p>Your certificate of completion is now ready. You can easily generate and download it directly from the portal by following these steps:</p>
    <ol>
        <li><strong>Visit the Portal:</strong> Go to <a href="{link_url}">Your Personal Portal</a>.</li>
        <li><strong>Enter Your Details:</strong> Type in your Full Name and the School you represent.</li>
        <li><strong>Download:</strong> Click the Download button, and your certificate will automatically save to your device's downloads folder.</li>
    </ol>
    
    <p><strong>Note:</strong> Please double-check the spelling of your name and school before submitting to ensure your certificate generates accurately. If you run into any issues accessing the portal or downloading your file, feel free to reply directly to this email or contact us at <a href="mailto:asimiyad.troffee@gmail.com">asimiyad.troffee@gmail.com</a>.</p>
    
    <p>Thank you once again for your commitment to education. We look forward to seeing you at future workshops!</p>
    <p>Warm regards,<br>The Event Team</p>
    """

    text_body = f"Dear Teachers,\n\nThank you for participating in the {event_name}! We truly appreciate your time, engagement, and dedication to continuous learning. Your active involvement made the event a resounding success.\n\nHow to Download Your Certificate\nYour certificate of completion is now ready. You can easily generate and download it directly from the portal by following these steps:\n\n1. Visit the Portal: Go to {link_url}\n2. Enter Your Details: Type in your Full Name and the School you represent.\n3. Download: Click the Download button, and your certificate will automatically save to your device's downloads folder.\n\nNote: Please double-check the spelling of your name and school before submitting to ensure your certificate generates accurately. If you run into any issues accessing the portal or downloading your file, feel free to reply directly to this email or contact us at asimiyad.troffee@gmail.com.\n\nThank you once again for your commitment to education. We look forward to seeing you at future workshops!\n\nWarm regards,\nThe Event Team"

    job = EmailJob(
        to_email=req.teacher_email,
        subject=f"Thank You for Attending {event_name} | Download Your Certificate",
        html_body=html_msg,
        text_body=text_body,
        **smtp_cfg,
    )

    try:
        await send_single_email_async(job)
    except Exception as e:
        try:
            client.table("teacher_links").update({
                "email_bounced": True,
                "bounce_error": str(e)[:500]
            }).eq("token", token_val).execute()
        except Exception:
            pass
        raise HTTPException(500, f"Email failed: {e}")

    return {"ok": True, "token": token_val, "message": f"Link resent to {req.teacher_email}"}


class ResendEventLinksRequest(BaseModel):
    event_id: str = Field(..., min_length=1)
    custom_message: str = ""
    email_choice: str = "default"
    custom_subject: str = ""

@router.post("/resend-event-links")
async def resend_event_links(req: ResendEventLinksRequest):
    """Resend invitation links for all pending teachers in an event.
    
    Returns HTTP 202 immediately and sends emails in the background.
    """
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    event_res = client.table("events").select("name, event_type").eq("id", req.event_id).limit(1).execute()
    if not event_res.data:
        raise HTTPException(404, "Event not found")
    event_name = event_res.data[0]["name"]
    event_type = event_res.data[0].get("event_type", "student")

    # Get all pending teachers for this event
    links_res = client.table("teacher_links").select("*").eq("event_id", req.event_id).eq("used", False).execute()
    links = links_res.data or []
    if not links:
        return {"ok": True, "message": "No pending teachers to resend to."}

    smtp_cfg = _get_smtp_config(client)
    raw_sender = smtp_cfg.get("sender_name", "")
    email_only = raw_sender.split("<")[1].split(">")[0] if "<" in raw_sender else raw_sender
    smtp_cfg["sender_name"] = f"{event_name} <{email_only}>"

    base_url = _get_public_url(client)
    subject = f"Reminder: Generate certificates for {event_name}"

    # Build email jobs for all pending teachers
    jobs = []
    for link in links:
        token_val = link["token"]
        teacher_email = link["teacher_email"]
        teacher_name = link.get("teacher_name") or "Teacher"
        html_page = "teacher-cert.html" if event_type == "teacher" else "teacher.html"
        link_url = f"{base_url}/{html_page}?t={token_val}"

        msg = req.custom_message.replace("{name}", teacher_name)
        magic_link_html = f'<a href="{link_url}" style="display:inline-block; padding:10px 15px; background:#2563EB; color:#fff; text-decoration:none; border-radius:6px; font-weight:bold;">Access Your Portal Here</a>'

        if req.email_choice == "custom" and msg:
            if "{magiclink}" in msg:
                msg = msg.replace("{magiclink}", magic_link_html)
                html_msg = f"""
                <div style='font-size: 16px; font-family: sans-serif; color: #333;'>
                    {msg}
                </div>
                """
            else:
                html_msg = f"""
                <div style='font-size: 16px; font-family: sans-serif; color: #333;'>
                    {msg}
                </div>
                <br>
                <p>👉 <strong>{magic_link_html}</strong></p>
                """
            
            clean_text = re.sub(r'<[^>]+>', '', msg).strip()
            text_body = f"{clean_text}\n\nLink: {link_url}"
        else:
            html_msg = f"""
            <p>Dear Teachers,</p>
            <p>Thank you for participating in the <strong>{event_name}</strong>! We truly appreciate your time, engagement, and dedication to continuous learning. Your active involvement made the event a resounding success.</p>
            
            <p><strong>How to Download Your Certificate</strong></p>
            <p>Your certificate of completion is now ready. You can easily generate and download it directly from the portal by following these steps:</p>
            <ol>
                <li><strong>Visit the Portal:</strong> Go to <a href="{link_url}">Your Personal Portal</a>.</li>
                <li><strong>Enter Your Details:</strong> Type in your Full Name and the School you represent.</li>
                <li><strong>Download:</strong> Click the Download button, and your certificate will automatically save to your device's downloads folder.</li>
            </ol>
            
            <p><strong>Note:</strong> Please double-check the spelling of your name and school before submitting to ensure your certificate generates accurately. If you run into any issues accessing the portal or downloading your file, feel free to reply directly to this email or contact us at <a href="mailto:asimiyad.troffee@gmail.com">asimiyad.troffee@gmail.com</a>.</p>
            
            <p>Thank you once again for your commitment to education. We look forward to seeing you at future workshops!</p>
            <p>Warm regards,<br>The Event Team</p>
            """
            text_body = f"Dear Teachers,\n\nThank you for participating in the {event_name}! We truly appreciate your time, engagement, and dedication to continuous learning. Your active involvement made the event a resounding success.\n\nHow to Download Your Certificate\nYour certificate of completion is now ready. You can easily generate and download it directly from the portal by following these steps:\n\n1. Visit the Portal: Go to {link_url}\n2. Enter Your Details: Type in your Full Name and the School you represent.\n3. Download: Click the Download button, and your certificate will automatically save to your device's downloads folder.\n\nNote: Please double-check the spelling of your name and school before submitting to ensure your certificate generates accurately. If you run into any issues accessing the portal or downloading your file, feel free to reply directly to this email or contact us at asimiyad.troffee@gmail.com.\n\nThank you once again for your commitment to education. We look forward to seeing you at future workshops!\n\nWarm regards,\nThe Event Team"

        job_subject = req.custom_subject if (req.email_choice == "custom" and req.custom_subject) else f"Thank You for Attending {event_name} | Download Your Certificate"

        jobs.append(EmailJob(
            to_email=teacher_email,
            subject=job_subject,
            html_body=html_msg,
            text_body=text_body,
            metadata={"token": token_val},
            **smtp_cfg,
        ))

    # Fire-and-forget: send all emails in the background
    asyncio.create_task(_background_bulk_email(jobs, req.event_id))

    return JSONResponse(
        status_code=202,
        content={"ok": True, "message": f"Sending {len(jobs)} emails in background...", "count": len(jobs)},
    )


class RetryRequest(BaseModel):
    sheet_row_number: int = Field(..., ge=2)


@router.post("/retry")
async def retry_submission(req: RetryRequest):
    """Re-process a failed sheet row."""
    from .google_sheets import get_new_responses
    from .form_watcher import _process_submission as watcher_process

    sheet_id = ""
    client = get_client()
    if client:
        r = client.table("config").select("value").eq("key", "form_sheet_id").limit(1).execute()
        if r.data:
            sheet_id = r.data[0].get("value", "")

    if not sheet_id:
        raise HTTPException(500, "FORM_SHEET_ID not configured")

    try:
        await mark_processed(sheet_id, req.sheet_row_number, "⏳ Retrying...")
    except Exception:
        pass

    # Read the specific row
    from .google_sheets import _get_sheets_service, extract_file_id
    service = _get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"Form Responses 1!A:E",
        valueRenderOption="FORMULA"
    ).execute()
    rows = result.get("values", [])
    if req.sheet_row_number >= len(rows) + 1:
        raise HTTPException(404, f"Row {req.sheet_row_number} not found")

    row = rows[req.sheet_row_number - 1]
    teacher_name = row[1].strip() if len(row) > 1 else ""
    teacher_email = row[2].strip() if len(row) > 2 else ""
    raw_file = row[3].strip() if len(row) > 3 else ""
    file_id = extract_file_id(raw_file) if raw_file else None

    if not teacher_name or not teacher_email or not file_id:
        raise HTTPException(400, "Incomplete row data")

    row_data = {
        "row_number": req.sheet_row_number,
        "teacher_name": teacher_name,
        "teacher_email": teacher_email,
        "file_id": file_id,
        "raw_file_cell": raw_file,
    }

    status = await watcher_process(row_data)

    try:
        await mark_processed(sheet_id, req.sheet_row_number, status)
    except Exception:
        pass

    return {"status": status}


@router.get("/config")
async def get_config():
    """Read all config from Supabase + .env fallback."""
    client = get_client()
    config_data = {}

    if client:
        r = client.table("config").select("*").execute()
        for item in r.data or []:
            config_data[item["key"]] = item["value"]

    # Add .env fallbacks
    config_data.setdefault("form_sheet_id", os.environ.get("FORM_SHEET_ID", ""))
    config_data.setdefault("poll_interval", os.environ.get("FORM_POLL_INTERVAL", "30"))
    config_data.setdefault("smtp_host", os.environ.get("SMTP_HOST", "smtp.hostinger.com"))
    config_data.setdefault("smtp_port", os.environ.get("SMTP_PORT", "587"))
    config_data.setdefault("smtp_secure", os.environ.get("SMTP_SECURE", "false"))
    config_data.setdefault("sender_email", os.environ.get("SMTP_USER", ""))
    config_data.setdefault("sender_password", os.environ.get("SMTP_PASS", ""))
    config_data.setdefault("sender_name", os.environ.get("SMTP_FROM", ""))

    return config_data


class ConfigUpdate(BaseModel):
    key: str = Field(..., min_length=1)
    value: str


@router.put("/config")
async def update_config(req: ConfigUpdate):
    """Update a single config value."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    client.table("config").upsert({"key": req.key, "value": req.value}).execute()
    return {"ok": True, "key": req.key, "value": req.value}


@router.get("/storage")
async def get_storage_usage():
    """Return storage usage for the certificates bucket."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    limit = 1073741824

    cfgs = client.table("config").select("*").execute()
    for item in cfgs.data or []:
        if item.get("key") == "storage_limit_bytes" and item.get("value"):
            try:
                limit = int(item["value"])
            except (ValueError, TypeError):
                pass

    total_bytes = 0
    total_files = 0

    def list_recursive(path: str = ""):
        nonlocal total_bytes, total_files
        try:
            items = client.storage.from_("certificates").list(path)
            for item in items:
                if item.get("id") is None:
                    list_recursive(item["name"])
                else:
                    meta = item.get("metadata") or {}
                    size_str = meta.get("size")
                    if size_str:
                        total_bytes += int(size_str)
                    total_files += 1
        except Exception as e:
            print(f"[storage] Error listing {path}: {e}")

    list_recursive()
    return {"used_bytes": total_bytes, "total_files": total_files, "limit_bytes": limit}
