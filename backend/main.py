import os
import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageFont as PILImageFont

from dotenv import load_dotenv

from . import state
from .config import load_config
from .certificate import generate_certificate
from .batch import run_batch, sse_event
from .google_sheets import fetch_names_from_sheet
from .email_sender import send_certificate_email
from .supabase_init import init_supabase, get_client
from .admin import router as admin_router
from .events_api import router as events_router
from .teacher_api import router as teacher_router
from .teacher_cert_api import router as teacher_cert_router

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
TEMPLATES_DIR = BASE_DIR / "templates"
FONTS_DIR = BASE_DIR / "fonts"
GENERATED_DIR = BASE_DIR / "generated"

FORM_SHEET_ID = os.environ.get("FORM_SHEET_ID", "")
FORM_POLL_INTERVAL = int(os.environ.get("FORM_POLL_INTERVAL", "30"))

_watcher_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    template_path = TEMPLATES_DIR / "template.png"
    if template_path.exists():
        state.template_image = Image.open(template_path)
    else:
        jpg_path = TEMPLATES_DIR / "template.jpg"
        if jpg_path.exists():
            state.template_image = Image.open(jpg_path)

    font_path = FONTS_DIR / "GreatVibes-Regular.ttf"
    if font_path.exists():
        state.cert_font = PILImageFont.truetype(str(font_path), 72)

    config_path = FRONTEND_DIR / "certificate-config.json"
    if config_path.exists():
        state.cert_config = load_config(str(config_path))

    # Seed default config in Supabase if empty
    from .supabase_init import get_client
    seed_client = get_client()
    if seed_client:
        existing = seed_client.table("config").select("key").limit(1).execute()
        if not existing.data:
            defaults = [
                {"key": "form_sheet_id", "value": os.environ.get("FORM_SHEET_ID", "")},
                {"key": "poll_interval", "value": os.environ.get("FORM_POLL_INTERVAL", "30")},
                {"key": "smtp_host", "value": os.environ.get("SMTP_HOST", "smtp.hostinger.com")},
                {"key": "smtp_port", "value": os.environ.get("SMTP_PORT", "587")},
                {"key": "smtp_secure", "value": os.environ.get("SMTP_SECURE", "false")},
                {"key": "sender_email", "value": os.environ.get("SMTP_USER", "")},
                {"key": "sender_password", "value": os.environ.get("SMTP_PASS", "")},
                {"key": "sender_name", "value": os.environ.get("SMTP_FROM", "")},
            ]
            seed_client.table("config").insert(defaults).execute()

    from .form_watcher import start_watcher
    global _watcher_task
    _watcher_task = asyncio.create_task(
        start_watcher(FORM_POLL_INTERVAL)
    )

    yield

    if _watcher_task:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass

    state.template_image = None
    state.cert_font = None
    state.cert_config = None


app = FastAPI(title="Certificate Generator", lifespan=lifespan)

allowed_origins_str = os.environ.get("ALLOWED_ORIGINS", "")
if allowed_origins_str:
    origins = [o.strip() for o in allowed_origins_str.split(",") if o.strip()]
else:
    origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(admin_router)
app.include_router(events_router)
app.include_router(teacher_router)
app.include_router(teacher_cert_router)


@app.get("/api/config")
async def get_config():
    if state.cert_config is None:
        raise HTTPException(404, "No certificate config loaded")
    return {
        "templateWidth": state.cert_config.templateWidth,
        "templateHeight": state.cert_config.templateHeight,
        "text": {
            "x": state.cert_config.text.x,
            "y": state.cert_config.text.y,
            "fontFamily": state.cert_config.text.fontFamily,
            "fontSize": state.cert_config.text.fontSize,
            "fontColor": state.cert_config.text.fontColor,
            "textAlign": state.cert_config.text.textAlign,
        },
    }


class GenerateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    format: str = "png"


@app.post("/api/generate")
async def generate(req: GenerateRequest):
    if state.template_image is None:
        raise HTTPException(500, "No template loaded")
    if state.cert_font is None:
        raise HTTPException(500, "No font loaded")
    if state.cert_config is None:
        raise HTTPException(500, "No config loaded")

    fmt = req.format.lower()
    if fmt not in ("png", "pdf"):
        raise HTTPException(400, "format must be 'png' or 'pdf'")

    sanitized = req.name.strip()
    if not sanitized:
        raise HTTPException(400, "Name cannot be empty")

    font = PILImageFont.truetype(
        str(FONTS_DIR / "GreatVibes-Regular.ttf"),
        state.cert_config.text.fontSize,
    )

    result = generate_certificate(
        name=sanitized,
        template=state.template_image,
        font=font,
        config={
            "x": state.cert_config.text.x,
            "y": state.cert_config.text.y,
            "fontFamily": state.cert_config.text.fontFamily,
            "fontSize": state.cert_config.text.fontSize,
            "fontColor": state.cert_config.text.fontColor,
            "textAlign": state.cert_config.text.textAlign,
        },
        output_format=fmt,
    )

    media_type = "application/pdf" if fmt == "pdf" else "image/png"
    filename = f"Certificate-{sanitized.replace(' ', '-')}.{fmt}"

    return Response(
        content=result,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/template")
async def get_template():
    if state.template_image is None:
        raise HTTPException(404, "No template loaded")
    buf = BytesIO()
    state.template_image.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/api/preview")
async def preview(req: GenerateRequest):
    """Generate a preview image (returns PNG, always)."""
    if state.template_image is None:
        raise HTTPException(500, "No template loaded")
    if state.cert_font is None:
        raise HTTPException(500, "No font loaded")
    if state.cert_config is None:
        raise HTTPException(500, "No config loaded")

    sanitized = req.name.strip() or "Your Name"
    font = PILImageFont.truetype(
        str(FONTS_DIR / "GreatVibes-Regular.ttf"),
        state.cert_config.text.fontSize,
    )
    result = generate_certificate(
        name=sanitized,
        template=state.template_image,
        font=font,
        config={
            "x": state.cert_config.text.x,
            "y": state.cert_config.text.y,
            "fontFamily": state.cert_config.text.fontFamily,
            "fontSize": state.cert_config.text.fontSize,
            "fontColor": state.cert_config.text.fontColor,
            "textAlign": state.cert_config.text.textAlign,
        },
        output_format="png",
    )
    return Response(content=result, media_type="image/png")


@app.post("/api/upload")
async def upload(
    template: Optional[UploadFile] = File(None),
    config: Optional[str] = Form(None),
):
    if template and template.filename:
        ext = Path(template.filename).suffix.lower()
        dest = TEMPLATES_DIR / f"template{ext if ext in ('.png', '.jpg', '.jpeg') else '.png'}"
        content = await template.read()
        with open(dest, "wb") as f:
            f.write(content)
        state.template_image = Image.open(dest)

    if config:
        config_path = FRONTEND_DIR / "certificate-config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config)
        state.cert_config = load_config(str(config_path))

    return {"ok": True, "template": template is not None, "config": config is not None}


class BatchGenerateRequest(BaseModel):
    people: list[dict] = Field(..., min_length=1)
    format: str = "png"


class SheetFetchRequest(BaseModel):
    sheetId: str = Field(..., min_length=1)
    range: str = "Sheet1!A2:B"


@app.post("/api/batch/generate")
async def batch_generate(req: BatchGenerateRequest):
    if state.template_image is None:
        raise HTTPException(500, "No template loaded")
    if state.cert_font is None:
        raise HTTPException(500, "No font loaded")
    if state.cert_config is None:
        raise HTTPException(500, "No config loaded")

    fmt = req.format.lower()
    if fmt not in ("png", "pdf"):
        raise HTTPException(400, "format must be 'png' or 'pdf'")

    font_path = FONTS_DIR / "GreatVibes-Regular.ttf"

    config_dict = {
        "x": state.cert_config.text.x,
        "y": state.cert_config.text.y,
        "fontFamily": state.cert_config.text.fontFamily,
        "fontSize": state.cert_config.text.fontSize,
        "fontColor": state.cert_config.text.fontColor,
        "textAlign": state.cert_config.text.textAlign,
    }

    return StreamingResponse(
        run_batch(
            people=req.people,
            template=state.template_image,
            font_path=font_path,
            font_size=state.cert_config.text.fontSize,
            config=config_dict,
            output_format=fmt,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/batch/download/{filename:path}")
async def batch_download(filename: str):
    if not filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files can be downloaded")
    filepath = GENERATED_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, "File not found or expired")
    content = filepath.read_bytes()
    filepath.unlink(missing_ok=True)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/batch/from-sheet")
async def batch_from_sheet(req: SheetFetchRequest):
    try:
        names = fetch_names_from_sheet(
            sheet_id=req.sheetId, range_name=req.range
        )
        if not names:
            raise HTTPException(404, "No names found in the specified range")

        return {"names": names, "count": len(names)}
    except FileNotFoundError as e:
        raise HTTPException(500, str(e))
    except ImportError as e:
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(500, f"Failed to read sheet: {str(e)}")


class SendEmailsRequest(BaseModel):
    batch_id: str = Field(..., min_length=1)
    subject: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    sender_name: str = ""
    sender_email: str = ""
    smtp_password: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    people: list[dict] = []


@app.post("/api/batch/send-emails")
async def batch_send_emails(req: SendEmailsRequest):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    result = (
        client.table("certificates")
        .select("*")
        .eq("batch_id", req.batch_id)
        .eq("sent", False)
        .execute()
    )

    certs = result.data
    fallback = len(certs) == 0 and len(req.people) > 0

    if fallback:
        if state.template_image is None or state.cert_font is None or state.cert_config is None:
            raise HTTPException(500, "Template, font, or config not loaded")
        font = PILImageFont.truetype(
            str(FONTS_DIR / "GreatVibes-Regular.ttf"),
            state.cert_config.text.fontSize,
        )
        config_dict = {
            "x": state.cert_config.text.x, "y": state.cert_config.text.y,
            "fontFamily": state.cert_config.text.fontFamily,
            "fontSize": state.cert_config.text.fontSize,
            "fontColor": state.cert_config.text.fontColor,
            "textAlign": state.cert_config.text.textAlign,
        }
        total = len(req.people)
        output_format = "png"
    elif len(certs) == 0:
        raise HTTPException(404, "No unsent certificates found and no people provided")
    else:
        total = len(certs)
        output_format = certs[0].get("format", "png")

    async def event_stream():
        items = req.people if fallback else certs

        for i, item in enumerate(items, 1):
            if fallback:
                name = item.get("name", "").strip()
                email = item.get("email", "").strip()
            else:
                name = item.get("name", "")
                email = item.get("email", "")
                storage_path = item.get("storage_path", "")
                cert_format = item.get("format", "png")
                cert_id = item.get("id")

            if not name or not email:
                yield await sse_event("error_item", {
                    "current": i, "total": total, "name": name or "(empty)", "error": "Missing name or email",
                })
                continue

            subject = req.subject.replace("{name}", name)
            body = req.message.replace("{name}", name)

            try:
                if fallback:
                    img_bytes = generate_certificate(
                        name=name, template=state.template_image, font=font,
                        config=config_dict, output_format=output_format,
                    )
                    safe = name.replace(" ", "_").replace("/", "-").replace("\\", "-")
                    file_path = f"{req.batch_id}/{safe}_{i}.{output_format}"
                    client.storage.from_("certificates").upload(
                        path=file_path, file=img_bytes,
                        file_options={"content-type": f"image/{output_format}"},
                    )
                    client.table("certificates").insert({
                        "name": name, "email": email, "batch_id": req.batch_id,
                        "storage_path": file_path, "format": output_format, "sent": True,
                    }).execute()
                    cert_bytes = img_bytes
                else:
                    public_url = client.storage.from_("certificates").get_public_url(storage_path)
                    async with httpx.AsyncClient() as client_http:
                        resp = await client_http.get(public_url)
                        cert_bytes = resp.content

                attach_name = f"Certificate-{name.replace(' ', '_')}.{output_format}"
                send_certificate_email(
                    to_email=email, subject=subject,
                    html_body=body.replace("\n", "<br>"),
                    attachment_bytes=cert_bytes, attachment_name=attach_name,
                    from_email=req.sender_email,
                    smtp_password=req.smtp_password,
                    smtp_host=req.smtp_host,
                    smtp_port=req.smtp_port,
                )

                if not fallback:
                    client.table("certificates").update({
                        "sent": True, "sent_at": "now()",
                    }).eq("id", cert_id).execute()

                yield await sse_event("progress", {
                    "current": i, "total": total, "name": name, "email": email,
                    "status": "sent", "percent": round((i / total) * 100),
                })
            except Exception as e:
                yield await sse_event("error_item", {
                    "current": i, "total": total, "name": name, "email": email, "error": str(e),
                })

        yield await sse_event("complete", {"total": total, "batch_id": req.batch_id})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/batch/{batch_id}/status")
async def batch_status(batch_id: str):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    result = (
        client.table("certificates")
        .select("*")
        .eq("batch_id", batch_id)
        .execute()
    )

    items = result.data
    return {
        "batch_id": batch_id,
        "total": len(items),
        "sent": sum(1 for i in items if i.get("sent")),
        "certificates": items,
    }


class StudentCertificateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = ""
    format: str = "png"


@app.get("/api/student/certificate")
async def student_certificate_lookup(name: str = Query(..., min_length=1)):
    """Look up an existing certificate by student name."""
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    result = (
        client.table("certificates")
        .select("*")
        .eq("name", name.strip())
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    items = result.data
    if not items:
        raise HTTPException(404, "Certificate not found for this name")

    cert = items[0]
    storage_path = cert.get("storage_path")
    cert_format = cert.get("format", "png")
    if not storage_path:
        raise HTTPException(404, "Certificate file missing")

    public_url = get_client().storage.from_("certificates").get_public_url(storage_path)
    async with httpx.AsyncClient() as client_http:
        resp = await client_http.get(public_url)
        cert_bytes = resp.content

    media_type = "application/pdf" if cert_format == "pdf" else "image/png"
    filename = f"Certificate-{name.replace(' ', '_')}.{cert_format}"

    return Response(
        content=cert_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/student/certificate")
async def student_certificate_generate(req: StudentCertificateRequest):
    """Generate a certificate on-the-fly, save to Supabase, and return it."""
    if state.template_image is None or state.cert_font is None or state.cert_config is None:
        raise HTTPException(500, "Certificate template not loaded by admin yet")

    sanitized = req.name.strip()
    if not sanitized:
        raise HTTPException(400, "Name cannot be empty")

    fmt = req.format.lower()
    if fmt not in ("png", "pdf"):
        fmt = "png"

    font = PILImageFont.truetype(
        str(FONTS_DIR / "GreatVibes-Regular.ttf"),
        state.cert_config.text.fontSize,
    )

    config_dict = {
        "x": state.cert_config.text.x,
        "y": state.cert_config.text.y,
        "fontFamily": state.cert_config.text.fontFamily,
        "fontSize": state.cert_config.text.fontSize,
        "fontColor": state.cert_config.text.fontColor,
        "textAlign": state.cert_config.text.textAlign,
    }

    img_bytes = generate_certificate(
        name=sanitized,
        template=state.template_image,
        font=font,
        config=config_dict,
        output_format=fmt,
    )

    client = get_client()
    if client is not None:
        try:
            import uuid
            batch_id = uuid.uuid4().hex[:12]
            safe = sanitized.replace(" ", "_").replace("/", "-").replace("\\", "-")
            file_path = f"student/{batch_id}/{safe}.{fmt}"
            client.storage.from_("certificates").upload(
                path=file_path,
                file=img_bytes,
                file_options={"content-type": f"image/{fmt}"},
            )
            client.table("certificates").insert({
                "name": sanitized,
                "email": req.email or "",
                "batch_id": batch_id,
                "storage_path": file_path,
                "format": fmt,
                "sent": False,
            }).execute()
        except Exception as e:
            print(f"[student] Failed to save certificate to DB: {e}")

    media_type = "application/pdf" if fmt == "pdf" else "image/png"
    filename = f"Certificate-{sanitized.replace(' ', '_')}.{fmt}"

    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/watcher/status")
async def watcher_status():
    if not FORM_SHEET_ID:
        return {"active": False, "message": "FORM_SHEET_ID not configured in .env"}
    from .form_watcher import get_watcher_stats
    stats = get_watcher_stats()
    return {
        "active": _watcher_task is not None and not _watcher_task.done(),
        "form_sheet_id": FORM_SHEET_ID,
        "poll_interval": FORM_POLL_INTERVAL,
        "stats": stats,
    }


@app.get("/admin")
async def admin_page():
    return RedirectResponse(url="/index.html")


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
