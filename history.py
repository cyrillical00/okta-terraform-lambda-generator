import json
import os
from datetime import datetime, timezone

HISTORY_FILE = ".streamlit/command_history.json"
MAX_ENTRIES_PER_USER = 50


def _load() -> dict:
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def add_entry(email: str, user_input: str, intent: dict) -> None:
    data = _load()
    entries = data.get(email, [])
    if entries and entries[0].get("input") == user_input:
        return
    entries.insert(0, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "input": user_input,
        "operation_type": intent.get("operation_type", ""),
        "resource_type": intent.get("resource_type", ""),
        "resource_name": intent.get("resource_name", ""),
    })
    data[email] = entries[:MAX_ENTRIES_PER_USER]
    _save(data)


def get_entries(email: str) -> list[dict]:
    return _load().get(email, [])
