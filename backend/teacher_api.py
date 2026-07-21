import json
import uuid
import zipfile
import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
import httpx
from PIL import Image, ImageFont as PILImageFont

from .supabase_init import get_client
from .certificate import generate_certificate
from .batch import sse_event
from . import state

router = APIRouter(prefix="/api/teacher")

FONTS_DIR = Path(__file__).resolve().parent / "fonts"


@router.get("/link/{token}")
async def get_link_info(token: str):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    # Fetch token info
    res = client.table("teacher_links").select("*, events(*)").eq("token", token).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Invalid or expired token")
        
    link_data = res.data[0]
    event_data = link_data.get("events")
    
    if not event_data:
        raise HTTPException(404, "Event not found for this token")

    return {
        "teacher_name": link_data.get("teacher_name"),
        "teacher_email": link_data.get("teacher_email"),
        "event_name": event_data.get("name"),
        "event_id": event_data.get("id"),
        "used": link_data.get("used"),
        "fields": event_data.get("config_json", {}).get("fields", [])
    }

class TeacherGenerateRequest(BaseModel):
    token: str = Field(..., min_length=1)
    records: list[dict] = Field(..., min_length=1)

@router.post("/generate")
async def generate_teacher_certificates(req: TeacherGenerateRequest):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    # 1. Validate token and get event info
    res = client.table("teacher_links").select("*, events(*)").eq("token", req.token).limit(1).execute()
    if not res.data:
        raise HTTPException(404, "Invalid token")
        
    link_data = res.data[0]
    event_data = link_data.get("events")
    
    if not event_data:
        raise HTTPException(404, "Event not found")
        
    template_path = event_data.get("template_storage_path")
    config = event_data.get("config_json", {})
    
    if not template_path:
        raise HTTPException(500, "Event has no template")

    # 2. Download template image
    public_url = client.storage.from_("certificates").get_public_url(template_path)
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(public_url)
            resp.raise_for_status()
            template_bytes = resp.content
            template_image = Image.open(BytesIO(template_bytes))
    except Exception as e:
        raise HTTPException(500, f"Failed to load template image: {e}")

    fonts = {
        "Great Vibes": lambda size: PILImageFont.truetype(str(FONTS_DIR / "GreatVibes-Regular.ttf"), size),
        "Inter": lambda size: PILImageFont.truetype(str(FONTS_DIR / "Inter-Regular.ttf"), size) if (FONTS_DIR / "Inter-Regular.ttf").exists() else PILImageFont.truetype(str(FONTS_DIR / "GreatVibes-Regular.ttf"), size)
    }
    
    total = len(req.records)
    
    # 3. Mark link as used
    client.table("teacher_links").update({"used": True}).eq("id", link_data["id"]).execute()

    async def event_stream():
        batch_id = link_data["id"]
        zip_filename = f"certificates_{batch_id}.zip"
        generated_dir = Path(__file__).resolve().parent / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        zip_path = generated_dir / zip_filename

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for i, record in enumerate(req.records, 1):
                # Try to find something that looks like a name for logging and filename
                primary_name = None
                for k, v in record.items():
                    kl = str(k).lower()
                    if "name" in kl or "nama" in kl:
                        primary_name = v
                        break
                
                if not primary_name:
                    primary_name = f"Certificate_{i}"
                    
                if isinstance(primary_name, str):
                    primary_name = primary_name.strip()
                if not primary_name:
                    primary_name = f"Certificate_{i}"
                    
                try:
                    # Acquire the semaphore to limit concurrent CPU-bound work.
                    # If 50 teachers are generating at once, only 5 will run in
                    # parallel; the rest queue here until a slot opens up.
                    async with state.cert_gen_semaphore:
                        img_bytes = await asyncio.to_thread(
                            generate_certificate,
                            data=record,
                            template=template_image,
                            fonts=fonts,
                            config=config,
                            output_format="pdf",
                        )
                    
                    safe_name = str(primary_name).replace(" ", "_").replace("/", "-").replace("\\", "-")
                    arcname = f"certificates/{safe_name}.pdf"
                    zf.writestr(arcname, img_bytes)

                    # Save to Supabase for tracking
                    file_path = f"teacher/{batch_id}/{safe_name}_{i}.pdf"
                    try:
                        client.storage.from_("certificates").upload(
                            path=file_path,
                            file=img_bytes,
                            file_options={"content-type": "application/pdf"},
                        )
                        email_val = record.get("email") or record.get("Email") or record.get("emel") or ""
                        client.table("certificates").insert({
                            "name": primary_name,
                            "email": str(email_val),
                            "batch_id": batch_id,
                            "storage_path": file_path,
                            "format": "pdf",
                            "sent": False,
                        }).execute()
                    except Exception as db_e:
                        print(f"[teacher_api] DB save error: {db_e}")


                    yield await sse_event("progress", {
                        "current": i,
                        "total": total,
                        "name": primary_name,
                        "percent": round((i / total) * 100),
                    })
                except Exception as e:
                    yield await sse_event("error_item", {
                        "current": i, "total": total, "name": primary_name, "error": str(e),
                    })

        # Track the generated count in the event's config_json
        current_count = config.get("generated_count", 0)
        config["generated_count"] = current_count + total
        client.table("events").update({"config_json": config}).eq("id", link_data["event_id"]).execute()

        yield await sse_event("complete", {
            "download_url": f"/api/batch/download/{zip_filename}",
            "batch_id": batch_id,
            "total": total,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
