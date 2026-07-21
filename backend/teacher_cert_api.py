from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx
from io import BytesIO
from PIL import Image
import uuid
from datetime import datetime

from .supabase_init import get_client, get_public_url
from .certificate import generate_certificate

router = APIRouter(prefix="/api/teacher-cert")

class TeacherCertRequest(BaseModel):
    token: str
    teacher_name: str
    school_name: str

@router.post("/generate")
async def generate_teacher_cert(req: TeacherCertRequest):
    client = get_client()
    if not client:
        raise HTTPException(500, "Database not configured")

    # 1. Verify token
    link_res = client.table("teacher_links").select("*").eq("token", req.token).limit(1).execute()
    if not link_res.data:
        raise HTTPException(404, "Invalid or expired link")
    
    link = link_res.data[0]
    event_id = link["event_id"]

    # 2. Get event & verify it's a teacher event
    event_res = client.table("events").select("*").eq("id", event_id).limit(1).execute()
    if not event_res.data:
        raise HTTPException(404, "Event not found")
    
    event = event_res.data[0]
    if event.get("event_type") != "teacher":
        raise HTTPException(400, "This link is not for a Teacher event")

    template_path = event.get("teacher_template_path") or event.get("template_storage_path")
    if not template_path:
        raise HTTPException(400, "No teacher template found for this event")

    config = event.get("config_json", {})

    # 2.5 Update the teacher's record in the database
    try:
        client.table("teacher_links").update({
            "used": True,
            "teacher_name": req.teacher_name,
            "school": req.school_name,
            "completed_at": datetime.utcnow().isoformat()
        }).eq("token", req.token).execute()
    except Exception as e:
        print(f"Failed to update teacher record: {e}")

    # 3. Download template
    template_url = get_public_url("certificates", template_path)
    async with httpx.AsyncClient() as http:
        resp = await http.get(template_url)
        if resp.status_code != 200:
            raise HTTPException(500, "Failed to download teacher template")
        template_bytes = resp.content

    try:
        img = Image.open(BytesIO(template_bytes))
    except Exception as e:
        raise HTTPException(500, "Invalid image format")

    # 4. Map fields and Generate certificate
    # Ensure both "Teacher Name" / "Name" and "School Name" / "School" map correctly 
    # to whatever the user defined in their config fields.
    data = {
        "Name": req.teacher_name,
        "Teacher Name": req.teacher_name,
        "teacher_name": req.teacher_name,
        "School": req.school_name,
        "School Name": req.school_name,
        "school_name": req.school_name
    }

    try:
        pdf_bytes = generate_certificate(
            template=img,
            config=config,
            output_format="pdf",
            data=data
        )
    except Exception as e:
        raise HTTPException(500, f"Generation failed: {e}")

    # 5. Save to Supabase Storage
    out_id = uuid.uuid4().hex[:8]
    safe_name = req.teacher_name.replace(" ", "_").replace("/", "-")
    storage_path = f"teacher_certs/{event_id}/{safe_name}_{out_id}.pdf"

    try:
        client.storage.from_("certificates").upload(
            path=storage_path,
            file=pdf_bytes,
            file_options={"content-type": "application/pdf"}
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to upload certificate: {e}")

    download_url = get_public_url("certificates", storage_path)
    return {"ok": True, "download_url": download_url}
