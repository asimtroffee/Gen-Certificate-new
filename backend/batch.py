import json
import uuid
import zipfile
import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFont as PILImageFont

from .certificate import generate_certificate
from .supabase_init import init_supabase, get_client, get_public_url
from . import state

STORAGE_BUCKET = "certificates"
GENERATED_DIR = Path(__file__).resolve().parent / "generated"


async def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def upload_to_supabase(
    name: str,
    email: str,
    file_bytes: bytes,
    batch_id: str,
    output_format: str,
    index: int,
) -> str | None:
    client = get_client()
    if client is None:
        return None

    safe = name.replace(" ", "_").replace("/", "-").replace("\\", "-")
    file_path = f"{batch_id}/{safe}_{index}.{output_format}"

    client.storage.from_(STORAGE_BUCKET).upload(
        path=file_path,
        file=file_bytes,
        file_options={"content-type": f"image/{output_format}"},
    )

    public_url = get_public_url(STORAGE_BUCKET, file_path)

    client.table("certificates").insert({
        "name": name,
        "email": email,
        "batch_id": batch_id,
        "storage_path": file_path,
        "format": output_format,
        "sent": False,
    }).execute()

    return public_url


async def run_batch(
    people: list[dict],
    template: Image.Image,
    font_path: Path,
    font_size: int,
    config: dict,
    output_format: str = "png",
):
    total = len(people)
    if total == 0:
        yield await sse_event("error", {"message": "No people provided"})
        return

    task_id = uuid.uuid4().hex[:12]
    batch_id = task_id
    zip_filename = f"certificates_{task_id}.zip"
    zip_path = GENERATED_DIR / zip_filename
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    font = PILImageFont.truetype(str(font_path), font_size)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, person in enumerate(people, 1):
            name = person.get("name", "").strip()
            email = person.get("email", "").strip()
            if not name:
                yield await sse_event("error_item", {
                    "current": i, "total": total, "name": name or "(empty)", "error": "Empty name",
                })
                continue

            try:
                # Acquire semaphore to limit concurrent CPU-bound Pillow work,
                # then run the actual image generation in a thread.
                async with state.cert_gen_semaphore:
                    img_bytes = await asyncio.to_thread(
                        generate_certificate,
                        name=name,
                        template=template,
                        font=font,
                        config=config,
                        output_format=output_format,
                    )

                safe_name = name.replace(" ", "_").replace("/", "-").replace("\\", "-")
                arcname = f"certificates/{safe_name}.{output_format}"
                zf.writestr(arcname, img_bytes)

                storage_url = None
                if email:
                    storage_url = await upload_to_supabase(
                        name=name,
                        email=email,
                        file_bytes=img_bytes,
                        batch_id=batch_id,
                        output_format=output_format,
                        index=i,
                    )

                yield await sse_event("progress", {
                    "current": i,
                    "total": total,
                    "name": name,
                    "email": email,
                    "storageUrl": storage_url,
                    "percent": round((i / total) * 100),
                })
            except Exception as e:
                yield await sse_event("error_item", {
                    "current": i, "total": total, "name": name, "error": str(e),
                })

    yield await sse_event("complete", {
        "download_url": f"/api/batch/download/{zip_filename}",
        "batch_id": batch_id,
        "total": total,
    })
