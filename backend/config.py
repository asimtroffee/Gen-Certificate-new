import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TextConfig:
    x: float = 50.0
    y: float = 45.0
    fontFamily: str = "Great Vibes"
    fontSize: int = 72
    fontColor: str = "#1a1a1a"
    textAlign: str = "center"

    def __post_init__(self):
        if not (0 <= self.x <= 100):
            raise ValueError(f"x must be 0-100, got {self.x}")
        if not (0 <= self.y <= 100):
            raise ValueError(f"y must be 0-100, got {self.y}")
        if self.fontSize <= 0 or self.fontSize > 500:
            raise ValueError(f"fontSize must be 1-500, got {self.fontSize}")
        if not re.match(r"^#[0-9a-fA-F]{6}$", self.fontColor):
            raise ValueError(f"fontColor must be hex like #1a1a1a, got {self.fontColor}")
        if self.textAlign not in ("left", "center", "right"):
            raise ValueError(f"textAlign must be left/center/right, got {self.textAlign}")


@dataclass
class CertificateConfig:
    templateWidth: int = 0
    templateHeight: int = 0
    text: TextConfig = field(default_factory=TextConfig)

    def __post_init__(self):
        if isinstance(self.text, dict):
            self.text = TextConfig(**self.text)
        if self.templateWidth <= 0 or self.templateHeight <= 0:
            pass


def load_config(path: str) -> CertificateConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return CertificateConfig(**data)
