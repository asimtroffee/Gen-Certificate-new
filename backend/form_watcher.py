"""
Background watcher: polls Google Form response sheets for active events.

Runs as an asyncio task within the FastAPI lifespan.
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone

from .email_sender import send_certificate_email
from .google_sheets import get_new_responses, mark_processed
from .supabase_init import get_client

_watcher_stats = {
    "last_run": None,
    "total_processed": 0,
    "total_errors": 0,
    "last_error": None,
    "is_running": False,
}


def get_watcher_stats() -> dict:
    return dict(_watcher_stats)


async def _process_submission(row: dict, event_id: str, event_name: str) -> str:
    """Process a single form response. Returns status text to write to sheet."""
    teacher_name = row["teacher_name"]
    teacher_email = row["teacher_email"]

    client = get_client()
    if client is None:
        return "❌ Error: Supabase client not initialized"

    token_val = str(uuid.uuid4())

    try:
        # Create teacher link
        client.table("teacher_links").insert({
            "token": token_val,
            "event_id": event_id,
            "teacher_name": teacher_name,
            "teacher_email": teacher_email
        }).execute()
    except Exception as e:
        print(f"[watcher] Failed to insert teacher link: {e}")
        return f"❌ Error: DB insert failed: {str(e)[:60]}"

    # Send Email
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.hostinger.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_secure = str(os.environ.get("SMTP_SECURE", "false")).lower() == "true"
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
            elif k == "smtp_secure" and v:
                smtp_secure = str(v).lower() == "true"
            elif k == "sender_name" and v:
                smtp_from = v
    except Exception:
        pass

    base_url = os.environ.get("PUBLIC_URL", "http://localhost:8000")
    link_url = f"{base_url}/teacher.html?t={token_val}"

    def _html_body(link):
        return f"""Hi {teacher_name},<br><br>You've requested access to generate certificates for <strong>{event_name}</strong>.<br><br>Click the secure link below to upload your student list and generate your certificates:<br><a href="{link}">{link}</a><br><br>Thanks,<br>Certificate System"""

    def _text_body(link):
        return f"""Hi {teacher_name},

You've requested access to generate certificates for {event_name}.

Click the secure link below to upload your student list and generate your certificates:
{link}

Thanks,
Certificate System"""

    subject = f"Your certificate generation link for {event_name}"

    def _send(hbody, tbody, subj):
        return send_certificate_email(
            to_email=teacher_email,
            subject=subj,
            html_body=hbody,
            text_body=tbody,
            attachment_bytes=None,
            attachment_name=None,
            from_email=smtp_user,
            smtp_password=smtp_pass,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_secure=smtp_secure,
            sender_name=smtp_from,
        )

    try:
        h = _html_body(link_url)
        t = _text_body(link_url)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _send(h, t, subject))

        return f"✅ Sent link to {teacher_email}"
    except Exception as e:
        return f"❌ Error sending email: {str(e)[:60]}"


async def start_watcher(global_poll_interval: int = 30):
    """Background task that polls all active events with a form_sheet_id."""
    _watcher_stats["is_running"] = True
    _watcher_stats["last_run"] = datetime.now(timezone.utc).isoformat()

    print("[watcher] Started multi-event watcher loop")
    client = get_client()

    while True:
        try:
            if client is None:
                print("[watcher] Waiting for Supabase client...")
                await asyncio.sleep(5)
                continue

            events = client.table("events").select("*").execute().data or []
            
            for event in events:
                config = event.get("config_json") or {}
                sheet_id = config.get("form_sheet_id")
                if not sheet_id:
                    continue
                
                event_name = event.get("name") or event.get("title") or "Unnamed Event"
                event_id = event.get("id")

                loop = asyncio.get_event_loop()
                try:
                    responses = await loop.run_in_executor(None, get_new_responses, sheet_id)
                except Exception as e:
                    print(f"[watcher] Failed to poll sheet {sheet_id} for event {event_name}: {e}")
                    continue

                for row in responses:
                    row_num = row["row_number"]
                    teacher = row["teacher_name"]

                    try:
                        await loop.run_in_executor(None, mark_processed, sheet_id, row_num, "⏳ Processing")
                    except Exception:
                        pass

                    print(f"[watcher] Processing row {row_num} for {event_name}: {teacher}")
                    status = await _process_submission(row, event_id, event_name)
                    await asyncio.sleep(1)

                    try:
                        await loop.run_in_executor(None, mark_processed, sheet_id, row_num, status)
                    except Exception as e:
                        print(f"[watcher] Failed to update sheet status: {e}")

                    if status.startswith("✅"):
                        _watcher_stats["total_processed"] += 1
                        print(f"[watcher] ✅ Row {row_num}: {status}")
                    else:
                        _watcher_stats["total_errors"] += 1
                        _watcher_stats["last_error"] = status
                        print(f"[watcher] ❌ Row {row_num}: {status}")

            _watcher_stats["last_run"] = datetime.now(timezone.utc).isoformat()

        except asyncio.CancelledError:
            print("[watcher] Stopped")
            _watcher_stats["is_running"] = False
            raise
        except Exception as e:
            _watcher_stats["last_error"] = str(e)
            print(f"[watcher] Global Error: {e}")

        await asyncio.sleep(global_poll_interval)
