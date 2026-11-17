"""No-network smoke tests for WB70-priority changes."""
import ast
from pathlib import Path
from datetime import date, datetime, time, timedelta
from collections import Counter
import pandas as pd
import numpy as np
from roster import *
from optimizer import *
from exports import generate_wfm_shift_record_csv

MORNING_SHIFT_CODES = ("0730-1630", "0800-1700", "0900-1800")
source = Path(__file__).with_name("app.py").read_text()
nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in (
    "shift_rule_defaults", "resolve_clock", "compute_breaks", "selected_shift_codes", "morning_group_label"
)]
exec(compile(ast.Module(body=nodes, type_ignores=[]), "functions", "exec"), globals())

DAY = "2026-11-17"

def rules(pu, code):
    d = shift_rule_defaults(DAY, code, pu)
    assert d["preset"] in ("Morning", "Mid", "Night")
    return {
        "earliest": d["earliest"].strftime("%H:%M"),
        "final": d["final"].strftime("%H:%M"),
        "meal_start": d["meal_start"].strftime("%H:%M"),
        "meal_end": d["meal_end"].strftime("%H:%M"),
        "min_gap": 45 if pu == "HLC" else 60,
        "max_gap": 105 if pu == "HLC" else 90,
        "allow_wb70_second_half": d["preset"] in ("Morning", "Night"),
        "durations": {"Short": 15, "Meal": 30, "WB20": 20, "WB70": 70},
    }

def person(name, code, fixed="", ticket=False):
    return {
        "Name": name, "Email": name.replace(" ", "").lower() + "@example.test",
        "ShiftCode": code, "Include": True, "WB70": bool(fixed),
        "Ticket Moderator": ticket, "Fixed WB70 Start": fixed,
        "WB70 Duration (mins)": "",
    }

assert shift_rule_defaults(DAY, "0730-1630", "HLC")["earliest"].strftime("%H:%M") == "08:15"
assert shift_rule_defaults(DAY, "0730-1630", "Agent")["earliest"].strftime("%H:%M") == "08:30"

for pu, code, fixed in [
    ("HLC", "0730-1630", "15:00"),
    ("HLC", "0730-1630", "08:00"),
    ("HLC", "1500-0000", "15:05"),
    ("HLC", "2330-0800", "06:45"),
    ("Agent", "1500-0000", "22:30"),
]:
    p = compute_breaks(DAY, pu, code, [person("Sample Moderator", code, fixed)], rules(pu, code))
    w = next(b for b in p["schedule"] if b["Resource"] == "WB70")
    assert w["Start"].strftime("%H:%M") == fixed
    assert (w["Finish"] - w["Start"]) == timedelta(minutes=70)
    assert p["wb70_priority_overrides"]
    assert "Moderation Task" in generate_wfm_shift_record_csv(p).decode("utf-8-sig")
    print("WB70 stays fixed:", pu, code, fixed)

p = compute_breaks(DAY, "HLC", "0730-1630", [
    person("Ticket A", "0730-1630", "10:00", ticket=True),
    person("Ticket B", "0730-1630", "10:00", ticket=True),
], rules("HLC", "0730-1630"))
assert p["wb70_ticket_coverage_exception"]
print("Overlapping fixed ticket WB70: reported")

for code in ("0730-1630", "1500-0000", "2330-0800"):
    pu = "Agent"
    d = rules(pu, code)
    p = compute_breaks(DAY, pu, code, [person("Without WB70", code)], d)
    assert not p["wb70_priority_overrides"]
    assert len(p["schedule"]) == 5
print("All regular-shift tests passed")
