"""Lark Base TSV and browser-only clipboard UI.

This module is intentionally independent from the optimizer, roster persistence,
Lark API, and all existing PNG/WFM/CSV export paths.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from html import escape
import base64
import hashlib
import re
import uuid
from exclusions import is_excluded_moderator

HEADERS = ("Email", "Date", "PU", "Shift", "Break 1", "Break 2",
           "Break 3", "Break 4", "Break 5", "Batch ID", "Status")
EMOJI_LABELS = {"Short": "☕ Short", "Meal": "🍽️ Meal", "WB20": "💙 Wellness", "WB70": "💙 Wellness"}
MORNING_CODES = frozenset(("0730-1630", "0800-1700", "0900-1800"))
EMAIL_PATTERN = re.compile(r"^[^\s@\t\r\n]+@[^\s@\t\r\n]+\.[^\s@\t\r\n]+$")


class LarkExportError(ValueError):
    """The generated plan is not safe to paste into Lark Base."""


def new_batch_id(selected_date: str, pu: str, shift: str) -> str:
    """Call once when a plan is generated. Keep the result in its payload."""
    date_text = date.fromisoformat(str(selected_date)).strftime("%Y%m%d")
    return f"{date_text}-{pu.upper()}-{shift.upper()}-{uuid.uuid4().hex[:12].upper()}"


def batch_id_from_payload(payload: dict) -> str:
    """Stable upgrade path for plans created before `batch_id` existed."""
    if payload.get("batch_id"):
        return str(payload["batch_id"])
    generated_at = str(payload.get("generated_at") or "")
    if not generated_at:
        raise LarkExportError("Batch ID bulunamadı. Lütfen planı yeniden oluşturun.")
    shift = _classify_shift(payload.get("shift_code", ""))
    identity = "|".join((str(payload.get("date")), str(payload.get("pu")),
                         str(payload.get("shift_code")), generated_at))
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
    return f"{date.fromisoformat(str(payload['date'])).strftime('%Y%m%d')}-{str(payload['pu']).upper()}-{shift.upper()}-{suffix}"


def _classify_shift(code: str) -> str:
    """Whitelist the planner's actual shift groups; never guess a custom shift."""
    code = str(code or "").strip().upper().replace(" ", "").replace(":", "")
    if code in MORNING_CODES:
        return "Morning"
    if code == "1500-0000":
        return "Mid"
    if code == "2330-0800":
        return "Night"
    raise LarkExportError(
        f"'{code or 'unknown'}' vardiyası Morning, Mid veya Night olarak doğrulanamadı. "
        "Yanlış Lark Base kategorisi oluşturmamak için kopyalama durduruldu."
    )


def _clean_field(value: object, label: str) -> str:
    text = str(value if value is not None else "").strip()
    if any(char in text for char in ("\t", "\r", "\n", "\x00")):
        raise LarkExportError(f"{label} içinde TAB, satır sonu veya geçersiz karakter var.")
    return text


def _break_cell(row: dict, shift_date: date) -> str:
    kind = row["Resource"]
    if kind not in EMOJI_LABELS:
        raise LarkExportError(f"Bilinmeyen mola türü: {kind!r}")
    start, finish = row.get("Start"), row.get("Finish")
    if not isinstance(start, datetime) or not isinstance(finish, datetime) or finish <= start:
        raise LarkExportError("Mola başlangıç veya bitiş zamanı geçersiz.")
    prefix_start = start.strftime("%d/%m ") if start.date() != shift_date else ""
    # If the break itself crosses midnight, display both dates explicitly.
    prefix_end = finish.strftime("%d/%m ") if finish.date() != start.date() else ""
    return f"{EMOJI_LABELS[kind]} {prefix_start}{start:%H:%M}-{prefix_end}{finish:%H:%M}"


def build_lark_base_export(payload: dict) -> tuple[str, list[dict]]:
    """Return pure TSV (without header/trailing newline) and structured preview.

    Validation is fail-closed: no incomplete or ambiguously identified employee
    can be included. Unlike CSV, this uses literal TAB and LF delimiters only.
    """
    schedule = payload.get("schedule")
    if not schedule:
        raise LarkExportError("Oluşturulmuş bir mola planı bulunamadı.")
    try:
        selected_date = date.fromisoformat(str(payload["date"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise LarkExportError("Planın vardiya başlangıç tarihi geçersiz.") from exc
    pu = _clean_field(payload.get("pu"), "PU")
