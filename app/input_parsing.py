from __future__ import annotations

import re
from datetime import time


def parse_time_input(value: str) -> time:
    text = value.strip()
    if re.fullmatch(r"\d{3,4}", text):
        hour_text, minute_text = text[:-2], text[-2:]
    else:
        parts = [item for item in re.split(r"[:.\s-]+", text) if item]
        if len(parts) != 2 or not all(item.isdigit() for item in parts):
            raise ValueError("invalid time format")
        hour_text, minute_text = parts
    hour = int(hour_text)
    minute = int(minute_text)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid time value")
    return time(hour, minute)

