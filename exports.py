"""Accessible, high-resolution PNG and CSV exports for Break Planner V11.2.
Focused on cleaner, more legible timetable visuals while preserving colorblind-safe cues.
"""
from __future__ import annotations
from datetime import timedelta
from io import BytesIO, StringIO
from zipfile import ZipFile, ZIP_DEFLATED
import csv
import math
import re

from PIL import Image, ImageDraw, ImageFont

# Accessible palette with strong contrast and text labels.
COLORS = {
    "Short": "#2F80ED",   # blue
    "Meal": "#E3A008",    # amber
    "WB20": "#14B8A6",    # teal
    "WB70": "#8B5CF6",    # violet
}
LABELS = {
    "Short": "S · Short",
    "Meal": "M · Meal",
    "WB20": "W20 · Wellness",
    "WB70": "W70 · Wellness",
}
TAG_SHORT = {"Short": "S", "Meal": "M", "WB20": "W20", "WB70": "W70"}

NAVY = "#102A43"
INK = "#1F2937"
MUTED = "#5B6B7A"
LINE = "#D8E1EA"
PALE = "#F6F9FC"
PALE_ALT = "#EEF4F9"
SURFACE = "#FFFFFF"
WHITE = "#FFFFFF"
SOFT_BADGE = "#EAF1F7"
TICKET_BG = "#183B56"
TICKET_FG = "#F8FBFF"

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(size: int, bold: bool = False):
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def fmt(dt):
    return dt.strftime("%H:%M")


def slug(value):
    result = re.sub(r"[^\w\-]+", "_", str(value or ""), flags=re.UNICODE).strip("_")
    return result or "NA"


def name_for(kind, payload, extension="png"):
    return f"{slug(kind)}_{slug(payload['date'])}_{slug(payload['pu'])}_{slug(payload['shift_code'])}.{extension}"


def _png_bytes(im: Image.Image):
    result = BytesIO()
    im.save(result, format="PNG", optimize=True)
    return result.getvalue()


def _bbox(draw, text, f):
    return draw.textbbox((0, 0), str(text), font=f)


def _text_size(draw, text, f):
    box = _bbox(draw, text, f)
    return box[2] - box[0], box[3] - box[1]


def _fit_text(draw, text, max_width, start_size=28, min_size=14, bold=False):
    text = str(text)
    size = start_size
    while size >= min_size:
        f = font(size, bold)
        w, _ = _text_size(draw, text, f)
        if w <= max_width:
            return text, f
        size -= 1
    f = font(min_size, bold)
    candidate = text
    while candidate:
        trial = candidate.rstrip() + "…"
        w, _ = _text_size(draw, trial, f)
        if w <= max_width:
            return trial, f
        candidate = candidate[:-1]
    return "…", f


def _draw_text_fit(draw, xy, text, max_width, start_size=28, min_size=14, bold=False, fill=INK):
    text2, f = _fit_text(draw, text, max_width, start_size, min_size, bold)
    draw.text(xy, text2, font=f, fill=fill)
    return f


def _centered_text(draw, box, text, size, bold=False, fill=WHITE):
    f = font(size, bold)
    w, h = _text_size(draw, text, f)
    x0, y0, x1, y1 = box
    draw.text((x0 + (x1 - x0 - w) / 2, y0 + (y1 - y0 - h) / 2 - 1), text, font=f, fill=fill)


def _rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _badge(draw, x, y, text, fill=SOFT_BADGE, text_fill=INK, outline=None, pad_x=18, pad_y=10, size=20, bold=False):
    f = font(size, bold)
    w, h = _text_size(draw, text, f)
    box = (x, y, x + w + pad_x * 2, y + h + pad_y * 2)
    _rounded(draw, box, 18, fill=fill, outline=outline or fill, width=1)
    draw.text((x + pad_x, y + pad_y - 1), text, font=f, fill=text_fill)
    return box[2]


def _legend_badge(draw, x, y, kind, label):
    color = COLORS[kind]
    f = font(19, True)
    txt_w, txt_h = _text_size(draw, label, f)
    w = txt_w + 84
    box = (x, y, x + w, y + 44)
    _rounded(draw, box, 14, fill=WHITE, outline=LINE, width=2)
    _rounded(draw, (x + 8, y + 8, x + 36, y + 36), 8, fill=color)
    draw.text((x + 46, y + 10), label, font=f, fill=INK)
    return box[2]


def _bar_text_color(kind):
    return INK if kind == "Meal" else WHITE


def _minutes_between(a, b):
    return (b - a).total_seconds() / 60.0


def generate_timetable_png(payload):
    schedule = payload["schedule"]
    shift_start = payload["shift_start_dt"]
    shift_end = payload["shift_end_dt"]
    total = _minutes_between(shift_start, shift_end)
    if total <= 0:
        raise ValueError("Shift end must be later than shift start")

    names = sorted({b["Name"] for b in schedule}, key=lambda s: s.casefold())

    # High-resolution canvas with more generous row height and name space.
    W = 3800
    name_col = 620
    right_margin = 70
    left_margin = 44
    row_h = 90
    top_header = 142
    meta_h = 86
    legend_h = 64
    timeline_head_h = 62
    top = top_header + meta_h + legend_h + timeline_head_h
    bottom_pad = 64
    H = max(980, top + len(names) * row_h + bottom_pad)

    im = Image.new("RGB", (W, H), PALE)
    d = ImageDraw.Draw(im)

    # Outer card surface
    _rounded(d, (18, 18, W - 18, H - 18), 26, fill=SURFACE, outline="#E7EEF5", width=2)
    d.rectangle((18, 18, W - 18, top_header), fill=NAVY)

    # Header
    d.text((58, 42), "PU BREAK TIMETABLE", font=font(42, True), fill=WHITE)
    sub = f"{payload['date']}   •   {payload['pu']}   •   {payload['shift_label']}   •   {fmt(shift_start)}–{fmt(shift_end)}"
    d.text((60, 95), sub, font=font(23), fill="#D7E3F0")

    right_x = W - 70
    if payload.get("tickets", 0):
        box_w = 260
        _rounded(d, (right_x - box_w, 42, right_x, 86), 16, fill="#184468")
        _centered_text(d, (right_x - box_w, 42, right_x, 86), f"Ticket mods: {payload['tickets']}", 20, True, WHITE)
        right_x -= box_w + 16
    _rounded(d, (right_x - 170, 42, right_x, 86), 16, fill="#1E4E78")
    _centered_text(d, (right_x - 170, 42, right_x, 86), payload["shift_code"], 20, True, WHITE)

    # Meta chips
    y_meta = top_header + 18
    x_meta = 56
    x_meta = _badge(d, x_meta, y_meta, f"Date: {payload['date']}", fill="#EEF5FB", text_fill=INK, outline=LINE, size=21, bold=True) + 12
    x_meta = _badge(d, x_meta, y_meta, f"Power Unit: {payload['pu']}", fill="#EEF5FB", text_fill=INK, outline=LINE, size=21, bold=True) + 12
    _badge(d, x_meta, y_meta, f"Shift: {payload['shift_label']}", fill="#EEF5FB", text_fill=INK, outline=LINE, size=21, bold=True)

    # Legend
    legend_y = top_header + meta_h + 10
    legend_x = 56
    for kind in ["Short", "Meal", "WB20", "WB70"]:
        legend_x = _legend_badge(d, legend_x, legend_y, kind, LABELS[kind]) + 14

    # Timetable grid coordinates
    x0 = left_margin + name_col
    x1 = W - right_margin
    # Name column header and timeline header backgrounds
    _rounded(d, (left_margin, top - timeline_head_h + 6, name_col + left_margin - 20, top - 6), 14, fill=PALE_ALT, outline=LINE, width=1)
    _rounded(d, (x0, top - timeline_head_h + 6, x1, top - 6), 14, fill=PALE_ALT, outline=LINE, width=1)
    d.text((left_margin + 26, top - 48), "Moderator", font=font(24, True), fill=INK)
    d.text((x0 + 18, top - 48), "Shift timeline", font=font(24, True), fill=INK)

    # Hour / half-hour markers
    for minute in range(0, int(total) + 1, 30):
        x = x0 + round(minute / total * (x1 - x0))
        thick = (minute % 60 == 0)
        line_color = "#B8C6D4" if thick else "#E0E7EE"
        d.line((x, top - 4, x, H - 38), fill=line_color, width=3 if thick else 1)
        label_dt = shift_start + timedelta(minutes=minute)
        time_text = fmt(label_dt)
        f = font(18, thick)
        w, _ = _text_size(d, time_text, f)
        d.text((x - w / 2, top - 40), time_text, font=f, fill=MUTED)

    # Rows
    for idx, name in enumerate(names):
        y = top + idx * row_h
        row_fill = WHITE if idx % 2 == 0 else "#FBFDFF"
        d.rectangle((left_margin, y, W - right_margin, y + row_h), fill=row_fill)
        d.line((left_margin, y + row_h, W - right_margin, y + row_h), fill=LINE, width=1)
        d.line((x0 - 18, y, x0 - 18, y + row_h), fill=LINE, width=2)

        breaks = sorted([b for b in schedule if b["Name"] == name], key=lambda b: b["Start"])
        is_ticket = any(b.get("TicketModerator") for b in breaks)

        # Name area
        name_x = left_margin + 18
        if is_ticket:
            _rounded(d, (name_x, y + 21, name_x + 36, y + 56), 10, fill=TICKET_BG)
            _centered_text(d, (name_x, y + 21, name_x + 36, y + 56), "T", 18, True, TICKET_FG)
            name_text_x = name_x + 52
            name_width = name_col - 110
        else:
            name_text_x = name_x
            name_width = name_col - 70
        _draw_text_fit(d, (name_text_x, y + 18), name, name_width, start_size=26, min_size=17, bold=True, fill=INK)

        # Bars
        for b in breaks:
            l = x0 + round(_minutes_between(shift_start, b["Start"]) / total * (x1 - x0))
            r = x0 + round(_minutes_between(shift_start, b["Finish"]) / total * (x1 - x0))
            if r - l < 8:
                continue
            kind = b["Resource"]
            color = COLORS[kind]
            box = (l + 3, y + 12, r - 3, y + row_h - 14)
            _rounded(d, box, 12, fill=color, outline="#17324D", width=2)
            text_fill = _bar_text_color(kind)

            # Three-line bar label if there is enough room; otherwise compact mode.
            width = box[2] - box[0]
            tag = TAG_SHORT[kind]
            if width >= 108:
                _centered_text(d, (box[0], box[1] + 2, box[2], box[1] + 24), tag, 15, True, text_fill)
                _centered_text(d, (box[0], box[1] + 20, box[2], box[1] + 42), fmt(b["Start"]), 13, True, text_fill)
                _centered_text(d, (box[0], box[1] + 40, box[2], box[1] + 60), fmt(b["Finish"]), 13, True, text_fill)
            elif width >= 70:
                compact = f"{tag} {fmt(b['Start'])}–{fmt(b['Finish'])}"
                txt, f = _fit_text(d, compact, width - 10, start_size=13, min_size=10, bold=True)
                tw, th = _text_size(d, txt, f)
                d.text((box[0] + (width - tw) / 2, box[1] + ((box[3] - box[1]) - th) / 2 - 1), txt, font=f, fill=text_fill)
            else:
                _centered_text(d, box, tag, 11, True, text_fill)

    footer = "All times local · S=Short · M=Meal · W20/W70=Wellness · T=Ticket moderator"
    d.text((left_margin, H - 34), footer, font=font(17), fill=MUTED)
    return _png_bytes(im)


# Colorblind-friendly Cividis-like gradient for heatmap, but rendered more elegantly.
CIVIDIS = ["#15314B", "#35586B", "#66736F", "#948B5E", "#C7AA48", "#F0D34B"]


def _hex_to_rgb(value):
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def _interp_color(percent):
    percent = max(0, min(1, float(percent)))
    u = percent * (len(CIVIDIS) - 1)
    i = min(int(u), len(CIVIDIS) - 2)
    frac = u - i
    a, b = _hex_to_rgb(CIVIDIS[i]), _hex_to_rgb(CIVIDIS[i + 1])
    return tuple(round(x + (y - x) * frac) for x, y in zip(a, b))


def generate_heatmap_png(payload):
    schedule = payload["schedule"]
    start = payload["earliest_dt"].replace(minute=0, second=0, microsecond=0)
    limit = payload["final_dt"]
    hours = []
    while start < limit:
        hours.append(start)
        start += timedelta(hours=1)

    cellw = 126
    cellh = 72
    x0 = 220
    top = 252
    W = 1920
    H = max(620, top + len(hours) * cellh + 130)
    im = Image.new("RGB", (W, H), PALE)
    d = ImageDraw.Draw(im)
    _rounded(d, (18, 18, W - 18, H - 18), 24, fill=WHITE, outline="#E8EEF4", width=2)
    d.rectangle((18, 18, W - 18, 146), fill=NAVY)
    d.text((48, 34), "CONCURRENT BREAKS · 5-MINUTE VIEW", font=font(33, True), fill=WHITE)
    d.text((48, 91), f"{payload['date']}   •   {payload['pu']}   •   {payload['shift_label']}   •   {fmt(payload['earliest_dt'])}–{fmt(payload['final_dt'])}", font=font(21), fill="#DBE8F4")

    counts = []
    for h in hours:
        row = []
        for m in range(0, 60, 5):
            t = h + timedelta(minutes=m)
            if not payload["earliest_dt"] <= t < payload["final_dt"]:
                row.append(None)
            else:
                row.append(sum(1 for b in schedule if b["Start"] <= t < b["Finish"]))
        counts.append(row)
    mx = max((v for row in counts for v in row if v is not None), default=1)

    _rounded(d, (34, top - 54, W - 36, H - 56), 18, fill="#FBFDFF", outline=LINE, width=1)
    for ci, m in enumerate(range(0, 60, 5)):
        label = f":{m:02d}"
        f = font(19, True)
        w, _ = _text_size(d, label, f)
        d.text((x0 + ci * cellw + (cellw - w) / 2, top - 35), label, font=f, fill=INK)

    for ri, h in enumerate(hours):
        y = top + ri * cellh
        d.text((40, y + 21), h.strftime("%H:00"), font=font(22, True), fill=INK)
        for ci, v in enumerate(counts[ri]):
            x = x0 + ci * cellw
            box = (x + 2, y + 2, x + cellw - 6, y + cellh - 6)
            if v is None:
                _rounded(d, box, 10, fill=PALE_ALT, outline="#D7E0E9", width=1)
                continue
            color = _interp_color(v / max(mx, 1))
            _rounded(d, box, 10, fill=color, outline=WHITE, width=2)
            lum = 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]
            fg = INK if lum > 150 else WHITE
            _centered_text(d, box, str(v), 28, True, fg)

    d.text((42, H - 78), f"Peak concurrent breaks: {mx}", font=font(22, True), fill=INK)
    d.text((42, H - 45), "Numbers are the primary indicator; color is a secondary visual aid. Grey cells are outside the allowed break window.", font=font(16), fill=MUTED)
    return _png_bytes(im)


def moderator_message(payload, name):
    rows = sorted([b for b in payload["schedule"] if b["Name"] == name], key=lambda b: b["Start"])
    labels = {"Short": "Short", "Meal": "Meal", "WB20": "Wellness 20", "WB70": "Wellness 70"}
    lines = [
        f"{name} — {payload['date']} · {payload['pu']} · {payload['shift_label']}",
        f"Shift: {fmt(payload['shift_start_dt'])}–{fmt(payload['shift_end_dt'])}",
    ]
    short_num = 0
    for b in rows:
        kind = b["Resource"]
        if kind == "Short":
            short_num += 1
            tag = f"Short {short_num}"
        else:
            tag = labels[kind]
        lines.append(f"{tag}: {fmt(b['Start'])}–{fmt(b['Finish'])}")
    return "\n".join(lines)


def generate_individual_card(payload, name):
    breaks = sorted([b for b in payload["schedule"] if b["Name"] == name], key=lambda b: b["Start"])
    W, H = 1400, 900
    im = Image.new("RGB", (W, H), PALE)
    d = ImageDraw.Draw(im)
    _rounded(d, (20, 20, W - 20, H - 20), 24, fill=WHITE, outline="#E8EEF4", width=2)
    d.rectangle((20, 20, W - 20, 172), fill=NAVY)
    _draw_text_fit(d, (60, 40), name, 1220, start_size=40, min_size=20, bold=True, fill=WHITE)
    d.text((60, 102), f"{payload['date']}   •   {payload['pu']}   •   {payload['shift_label']}   •   {fmt(payload['shift_start_dt'])}–{fmt(payload['shift_end_dt'])}", font=font(22), fill="#E0EAF2")
    d.text((60, 208), "MY BREAKS", font=font(30, True), fill=INK)
    count = 0
    for idx, b in enumerate(breaks):
        y = 266 + idx * 110
        fill = WHITE if idx % 2 == 0 else "#FAFCFE"
        _rounded(d, (52, y, 1348, y + 90), 16, fill=fill, outline=LINE, width=1)
        kind = b["Resource"]
        color = COLORS[kind]
        _rounded(d, (74, y + 17, 106, y + 73), 10, fill=color)
        if kind == "Short":
            count += 1
            desc = f"Short break {count}"
        elif kind == "Meal":
            desc = "Meal break"
        elif kind == "WB20":
            desc = "Wellness 20"
        else:
            desc = "Wellness 70"
        d.text((132, y + 26), desc, font=font(27, True), fill=INK)
        timetxt = f"{fmt(b['Start'])} – {fmt(b['Finish'])}"
        tw, _ = _text_size(d, timetxt, font(28, True))
        d.text((1300 - tw, y + 26), timetxt, font=font(28, True), fill=INK)
    d.text((58, H - 46), "Times shown in your local shift timezone · Please follow the latest shared PU schedule", font=font(18), fill=MUTED)
    return _png_bytes(im)


def generate_cards_zip(payload):
    buff = BytesIO()
    names = sorted({b["Name"] for b in payload["schedule"]}, key=str.casefold)
    with ZipFile(buff, "w", compression=ZIP_DEFLATED) as bundle:
        for i, name in enumerate(names, 1):
            bundle.writestr(f"Individual_Breaks/{i:03d}_{slug(name)}.png", generate_individual_card(payload, name))
        bundle.writestr("Moderator_break_summaries.txt", "\n\n".join(moderator_message(payload, name) for name in names).encode("utf-8"))
    return buff.getvalue()


def generate_csv(payload):
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "PU", "Shift", "Moderator", "Break type", "Start", "End", "Duration minutes", "Ticket moderator"])
    for b in sorted(payload["schedule"], key=lambda x: (x["Name"].casefold(), x["Start"])):
        writer.writerow([
            payload["date"], payload["pu"], payload["shift_label"], b["Name"], b["Resource"],
            fmt(b["Start"]), fmt(b["Finish"]), int((b["Finish"] - b["Start"]).total_seconds() / 60), bool(b.get("TicketModerator"))
        ])
    return out.getvalue().encode("utf-8-sig")
