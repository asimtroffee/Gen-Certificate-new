import asyncio
from typing import Optional

from PIL import Image, ImageFont

from .config import CertificateConfig

template_image: Optional[Image.Image] = None
cert_font: Optional[ImageFont] = None
cert_config: Optional[CertificateConfig] = None

# ── Shared concurrency controls ─────────────────────────────────────────────
# Limits concurrent CPU-bound Pillow image generation across ALL endpoints.
# Shared by teacher_api.py and batch.py to prevent CPU exhaustion.
cert_gen_semaphore = asyncio.Semaphore(5)
