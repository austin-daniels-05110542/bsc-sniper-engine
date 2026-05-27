"""Persist PairCreated detections for the web UI."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HISTORY_PATH = Path(__file__).resolve().parent / "data" / "pair_history.json"
_lock = threading.Lock()


def _ensure_file() -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not HISTORY_PATH.exists():
        HISTORY_PATH.write_text("[]", encoding="utf-8")


def load_history() -> List[Dict[str, Any]]:
    _ensure_file()
    with _lock:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))


def save_history(entries: List[Dict[str, Any]]) -> None:
    _ensure_file()
    with _lock:
        HISTORY_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def append_record(record: Dict[str, Any]) -> Dict[str, Any]:
    entries = load_history()
    record.setdefault("id", f"{record.get('tx_hash', '')}:{record.get('pair', '')}")
    record.setdefault("recorded_at", datetime.now(timezone.utc).isoformat())
    # Upsert by id
    entries = [e for e in entries if e.get("id") != record["id"]]
    entries.insert(0, record)
    save_history(entries)
    return record


def list_history(
    limit: int = 100,
    offset: int = 0,
    snipeable_only: bool = False,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    entries = load_history()
    if snipeable_only:
        entries = [e for e in entries if e.get("snipeable")]
    if source:
        entries = [e for e in entries if e.get("source") == source]
    total = len(entries)
    page = entries[offset : offset + limit]
    return {"total": total, "offset": offset, "limit": limit, "items": page}


def clear_history() -> int:
    entries = load_history()
    count = len(entries)
    save_history([])
    return count
