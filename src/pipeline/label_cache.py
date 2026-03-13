"""
Label profile cache — persists LLM-generated label synopses to avoid repeat API calls.

Stored as data/label_profiles.json: { "label name (lowercase)": "synopsis string" }
"""
import json
import os

from src.logger import get_logger

logger = get_logger(__name__)

_LABEL_PROFILES_FILE = "label_profiles.json"


def load_label_profiles(data_dir: str) -> dict[str, str]:
    path = os.path.join(data_dir, _LABEL_PROFILES_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"[label_cache] Loaded {len(data)} label synopses from {path}")
    return data


def save_label_profiles(profiles: dict[str, str], data_dir: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, _LABEL_PROFILES_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)
    logger.info(f"[label_cache] Saved {len(profiles)} label synopses to {path}")
