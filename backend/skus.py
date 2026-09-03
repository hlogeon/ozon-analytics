from __future__ import annotations

import re
import unicodedata


def normalize_sku(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    return re.sub(r"\s+", "", value)
