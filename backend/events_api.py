from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from typing import Optional
import json
import uuid
from .supabase_init import get_client

router = APIRouter(prefix="/api/events")


@router.post("")
async def create_event(
    name: str = Form(...),
    template: UploadFile = File(...),
    config: str = Form(...),
):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    if not template or not template.filename:
        raise HTTPException(400, "Template is required")

    try:
        config_data = json.loads(config)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid config JSON")

    content = await template.read()
    ext = template.filename.split(".")[-1].lower()
    if ext not in ("png", "jpg", "jpeg"):
        ext = "png"

    event_id = str(uuid.uuid4())
    storage_path = f"templates/{event_id}.{ext}"

    try:
        client.storage.from_("certificates").upload(
            path=storage_path,
            file=content,
            file_options={"content-type": f"image/{ext}"},
        )
    except Exception as e:
        print(f"Failed to upload template: {e}")
        raise HTTPException(500, f"Failed to upload template: {e}")

    try:
        result = (
            client.table("events")
            .insert(
                {
                    "id": event_id,
                    "name": name,
                    "template_storage_path": storage_path,
                    "config_json": config_data,
                }
            )
            .execute()
        )
        return {"ok": True, "event": result.data[0] if result.data else None}
    except Exception as e:
        print(f"Failed to save event: {e}")
        raise HTTPException(500, f"Failed to save event: {e}")


@router.put("/{event_id}")
async def update_event(
    event_id: str,
    name: str = Form(...),
    template: Optional[UploadFile] = File(None),
    config: str = Form(...),
):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    try:
        config_data = json.loads(config)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid config JSON")

    update_data = {"name": name, "config_json": config_data}

    if template and template.filename:
        content = await template.read()
        ext = template.filename.split(".")[-1].lower()
        if ext not in ("png", "jpg", "jpeg"):
            ext = "png"

        storage_path = f"templates/{event_id}_{uuid.uuid4().hex[:6]}.{ext}"

        try:
            client.storage.from_("certificates").upload(
                path=storage_path,
                file=content,
                file_options={"content-type": f"image/{ext}"},
            )
            update_data["template_storage_path"] = storage_path
        except Exception as e:
            raise HTTPException(500, f"Failed to upload new template: {e}")

    try:
        result = (
            client.table("events").update(update_data).eq("id", event_id).execute()
        )
        return {"ok": True, "event": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(500, f"Failed to update event: {e}")


@router.delete("/{event_id}")
async def delete_event(event_id: str):
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    # Fetch event first to get template path for cleanup
    template_path = None
    try:
        event_res = (
            client.table("events").select("template_storage_path").eq("id", event_id).limit(1).execute()
        )
        if event_res.data:
            template_path = event_res.data[0].get("template_storage_path")
    except Exception as e:
        print(f"[delete_event] Could not read template_storage_path: {e}")

    if template_path:
        try:
            client.storage.from_("certificates").remove([template_path])
        except Exception as e:
            print(f"[delete_event] Could not remove template from storage: {e}")

    try:
        client.table("events").delete().eq("id", event_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"Failed to delete event: {e}")


@router.get("")
async def list_events():
    client = get_client()
    if client is None:
        raise HTTPException(500, "Supabase not configured")

    result = (
        client.table("events").select("*").order("created_at", desc=True).execute()
    )
    events = result.data or []

    # Attach per-event link counts (graceful — table may not exist yet)
    link_counts: dict = {}
    used_counts: dict = {}
    try:
        link_res = client.table("teacher_links").select("event_id, used").execute()
        for lnk in (link_res.data or []):
            eid = lnk.get("event_id", "")
            link_counts[eid] = link_counts.get(eid, 0) + 1
            if lnk.get("used"):
                used_counts[eid] = used_counts.get(eid, 0) + 1
    except Exception as e:
        print(f"[list_events] Could not load link counts (run schema.sql?): {e}")

    for e in events:
        if e.get("template_storage_path"):
            e["template_url"] = client.storage.from_("certificates").get_public_url(
                e["template_storage_path"]
            )
        eid = e.get("id", "")
        e["links_sent"] = link_counts.get(eid, 0)
        e["links_used"] = used_counts.get(eid, 0)

    return {"events": events}
