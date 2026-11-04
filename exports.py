"""V11 exports using the original V10 Plotly visual designs.
Timetable and heatmap figures are ported directly from V10; individual views
use the same Plotly typography, white background, bars, and break colors.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from zipfile import ZipFile, ZIP_DEFLATED
import csv
import os
import re
import shutil
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

TIME_STEP=5

# Kaleido v1 uses a locally installed Chrome/Chromium browser to export PNGs.
# Streamlit Community Cloud installs Chromium from repository-root packages.txt.
# Explicitly advertise its binary path for Kaleido's browser discovery.
if not os.environ.get("BROWSER_PATH"):
    _chromium_binary = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
        or shutil.which("google-chrome-stable")
    )
    if _chromium_binary:
        os.environ["BROWSER_PATH"] = _chromium_binary


def fmt(dt):
    return dt.strftime("%H:%M")


def slug(value):
    result = re.sub(r"[^\w\-]+", "_", str(value or ""), flags=re.UNICODE).strip("_")
    return result or "NA"


def name_for(kind, payload, extension="png"):
    return f"{slug(kind)}_{slug(payload['date'])}_{slug(payload['pu'])}_{slug(payload['shift_code'])}.{extension}"


def _schedule_to_v10_dataframe(schedule):
    """Map V11 break records to the same fields V10's Plotly timeline uses."""
    result = []
    for b in schedule:
        start, finish = b["Start"], b["Finish"]
        duration = int((finish - start).total_seconds() / 60)
        if duration <= 20:
            bar_text = f"<b>{fmt(start)}<br>{fmt(finish)}</b>"
        else:
            bar_text = f"<b>{fmt(start)}-{fmt(finish)}</b>"
        result.append({
            "Task": str(b["Name"]),
            "Resource": b["Resource"],
            "Start": start,
            "Finish": finish,
            "Bar_Text": bar_text,
        })
    return pd.DataFrame(result, columns=["Task", "Resource", "Start", "Finish", "Bar_Text"]).sort_values(
        ["Task", "Start"], ascending=[False, True]
    )


def _render_png(fig, width, height):
    """Render the Plotly chart to the same 3x PNG output used in V10."""
    try:
        return fig.to_image(format="png", width=width, height=height, scale=3)
    except Exception as exc:
        raise RuntimeError(
            "High-resolution V10-style PNG rendering requires the pinned plotly/kaleido "
            "dependencies and a working Chrome/Chromium installation on the app server. "
            f"Underlying error: {exc}"
        ) from exc

def build_break_overlap_heatmap_matrix(schedule, earliest_dt, final_dt):
    """
    Build an hour x 5-minute matrix of concurrent breaks.

    Cells outside the allowed break window are NaN so they render blank.
    Values inside the window are calculated directly from the final optimized
    schedule, making the heatmap independent of solver internals.
    """
    if final_dt <= earliest_dt:
        return np.empty((0, 12)), [], []

    first_hour = earliest_dt.replace(minute=0, second=0, microsecond=0)
    last_active_point = final_dt - timedelta(minutes=1)
    last_hour = last_active_point.replace(minute=0, second=0, microsecond=0)

    hour_starts = []
    cursor = first_hour
    while cursor <= last_hour:
        hour_starts.append(cursor)
        cursor += timedelta(hours=1)

    minute_marks = list(range(0, 60, TIME_STEP))
    matrix = np.full((len(hour_starts), len(minute_marks)), np.nan, dtype=float)

    for row_idx, hour_start in enumerate(hour_starts):
        for col_idx, minute in enumerate(minute_marks):
            slot_dt = hour_start + timedelta(minutes=minute)
            if earliest_dt <= slot_dt < final_dt:
                matrix[row_idx, col_idx] = sum(
                    1 for b in schedule if b["Start"] <= slot_dt < b["Finish"]
                )

    row_labels = [dt.strftime("%H:00") for dt in hour_starts]
    col_labels = [f":{minute:02d}" for minute in minute_marks]
    return matrix, row_labels, col_labels

def create_break_overlap_heatmap(
    schedule,
    earliest_dt,
    final_dt,
    schedule_date=None,
    shift_name=None,
    power_unit=None,
):
    """Create the management-facing concurrent-break heatmap."""
    matrix, row_labels, col_labels = build_break_overlap_heatmap_matrix(
        schedule, earliest_dt, final_dt
    )

    if matrix.size == 0 or not np.any(~np.isnan(matrix)):
        return None, 0

    valid_values = matrix[~np.isnan(matrix)]
    z_min = float(np.min(valid_values))
    z_max = float(np.max(valid_values))

    # Avoid a zero-width color range when every valid cell has the same value.
    display_zmax = z_max if z_max > z_min else z_min + 1.0

    # Approved palette: very light dusty pink at the low end, moving through
    # rose/mauve into saturated deep violet for the highest concurrency.
    light_pink_to_violet = [
        [0.00, "#F8CDD8"],
        [0.20, "#F1B7CF"],
        [0.40, "#DF91C2"],
        [0.60, "#C261B4"],
        [0.80, "#9230A6"],
        [1.00, "#56006F"],
    ]

    fig = go.Figure(
        data=go.Heatmap(
            z=matrix,
            x=col_labels,
            y=row_labels,
            colorscale=light_pink_to_violet,
            zmin=z_min,
            zmax=display_zmax,
            colorbar=dict(
                title=dict(text="Concurrent<br>breaks", side="right"),
                tickmode="linear",
                dtick=1,
                thickness=26,
                len=1.0,
            ),
            hovertemplate=(
                "Hour: %{y}<br>"
                "5-minute interval: %{x}<br>"
                "Concurrent breaks: %{z:.0f}"
                "<extra></extra>"
            ),
            hoverongaps=False,
            showscale=True,
        )
    )

    # Add numeric labels with automatic black/white contrast.
    span = max(display_zmax - z_min, 1.0)
    for row_idx, row_label in enumerate(row_labels):
        for col_idx, col_label in enumerate(col_labels):
            value = matrix[row_idx, col_idx]
            if np.isnan(value):
                continue
            normalized = (float(value) - z_min) / span
            font_color = "white" if normalized >= 0.52 else "black"
            fig.add_annotation(
                x=col_label,
                y=row_label,
                text=f"<b>{int(value)}</b>",
                showarrow=False,
                font=dict(
                    family="Montserrat, sans-serif",
                    size=13,
                    color=font_color,
                ),
            )

    heatmap_height = max(520, len(row_labels) * 78 + 175)
    fig.update_layout(
        title=dict(
            text=(
                "<b>Concurrent Break Heatmap</b><br>"
                + (
                    f"<span style='font-size:13px'>Date: {schedule_date.strftime('%Y-%m-%d') if hasattr(schedule_date, 'strftime') else schedule_date} "
                    f"| Shift: {shift_name or '—'} | Power Unit: {power_unit or '—'} "
                    f"| Break Window: {earliest_dt.strftime('%H:%M')}–{final_dt.strftime('%H:%M')}</span>"
                    if schedule_date is not None or shift_name or power_unit
                    else f"Entire Shift ({earliest_dt.strftime('%H:%M')}–{final_dt.strftime('%H:%M')})"
                )
            ),
            x=0.5,
            xanchor="center",
            font=dict(family="Montserrat, sans-serif", size=21, color="black"),
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Montserrat, sans-serif", color="black", size=12),
        xaxis=dict(
            title="<b>5-minute interval</b>",
            side="bottom",
            showgrid=False,
            fixedrange=False,
        ),
        yaxis=dict(
            title="<b>Hour</b>",
            autorange="reversed",
            showgrid=False,
            fixedrange=False,
        ),
        margin=dict(l=75, r=90, t=110, b=65),
        height=heatmap_height,
    )

    return fig, heatmap_height

def create_timetable_figure(
    sched_df,
    schedule_date,
    shift_name,
    power_unit,
    shift_start_str,
    shift_end_str,
):
    """Build the timetable figure used by both the Planner and Calendar pages."""
    color_map = {
        "Short": "#3b82f6",
        "Meal": "#f97316",
        "WB20": "#22c55e",
        "WB70": "#a855f7",
    }

    fig = px.timeline(
        sched_df,
        x_start="Start",
        x_end="Finish",
        y="Task",
        color="Resource",
        text="Bar_Text",
        color_discrete_map=color_map,
    )
    fig.update_traces(
        textposition="inside",
        insidetextanchor="middle",
        textangle=0,
        textfont=dict(family="Montserrat, sans-serif", color="white", size=11),
        marker=dict(line=dict(width=1, color="rgba(255, 255, 255, 0.6)")),
    )

    # Plotly/Kaleido can clip all y-axis tick labels when margin.l=0.
    # Use explicit names anchored in a dedicated gutter, rendered as annotations,
    # rather than relying on auto-margins / category tick labels.
    moderator_names = sorted(sched_df["Task"].astype(str).unique().tolist(), key=str.casefold)
    num_moderators = len(moderator_names)
    dynamic_height = max(650, num_moderators * 45 + 210)
    # Allow enough room even for long names when a chart is exported as a PNG.
    name_gutter = max(260, min(490, 55 + max((len(n) for n in moderator_names), default=0) * 9))
    date_text = (
        schedule_date.strftime("%Y-%m-%d")
        if hasattr(schedule_date, "strftime")
        else str(schedule_date)
    )
    title_text = (
        "<b>Shift Break Timetable</b><br>"
        f"<span style='font-size:14px'>Date: {date_text} | Shift: {shift_name} | "
        f"Power Unit: {power_unit} | {shift_start_str}–{shift_end_str}</span>"
    )

    fig.update_layout(
        title=dict(
            text=title_text,
            x=0.5,
            xanchor="center",
            y=0.985,
            yanchor="top",
            font=dict(family="Montserrat, sans-serif", color="black", size=22),
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Montserrat, sans-serif", color="black", size=12),
        xaxis=dict(
            showgrid=True,
            gridcolor="#e5e5e5",
            tickformat="%H:%M",
            dtick=3600000,
            title="<b>Time</b>",
            side="bottom",
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#f3f4f6",
            title="",
            # Avoid automatic tick-label clipping across Plotly versions.
            showticklabels=False,
            categoryorder="array",
            categoryarray=moderator_names,
            autorange="reversed",
            fixedrange=True,
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.005,
            xanchor="center",
            x=0.5,
            title="",
        ),
        margin=dict(l=name_gutter, r=45, t=125, b=55),
        height=dynamic_height,
    )

    # Explicit y/category annotations are stable in both browser Plotly and
    # Kaleido PNG export. Keeping them inside the reserved left margin also
    # makes full-length moderator names legible in shared images.
    for person in moderator_names:
        fig.add_annotation(
            x=-0.015,
            xref="paper",
            y=person,
            yref="y",
            text=f"<b>{person}</b>",
            showarrow=False,
            xanchor="right",
            yanchor="middle",
            align="right",
            font=dict(family="Montserrat, sans-serif", size=14, color="#1c2838"),
        )
    return fig, dynamic_height


def generate_timetable_png(payload):
    df = _schedule_to_v10_dataframe(payload["schedule"])
    fig, dynamic_height = create_timetable_figure(
        df,
        payload["date"],
        payload["shift_label"],
        payload["pu"],
        fmt(payload["shift_start_dt"]),
        fmt(payload["shift_end_dt"]),
    )
    return _render_png(fig, width=1800, height=dynamic_height)


def generate_heatmap_png(payload):
    fig, dynamic_height = create_break_overlap_heatmap(
        payload["schedule"],
        payload["earliest_dt"],
        payload["final_dt"],
        schedule_date=payload["date"],
        shift_name=payload["shift_label"],
        power_unit=payload["pu"],
    )
    if fig is None:
        raise ValueError("No valid break window was found for the heatmap")
    return _render_png(fig, width=1800, height=dynamic_height)


def moderator_message(payload, name):
    rows = sorted((b for b in payload["schedule"] if b["Name"] == name), key=lambda b: b["Start"])
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
            label = f"Short {short_num}"
        else:
            label = labels[kind]
        lines.append(f"{label}: {fmt(b['Start'])}–{fmt(b['Finish'])}")
    return "\n".join(lines)


def _individual_figure(payload, name):
    """Single-person chart in the same original V10 Plotly style (V10 had no cards)."""
    rows = sorted([b for b in payload["schedule"] if b["Name"] == name], key=lambda b: b["Start"])
    if not rows:
        raise ValueError(f"No breaks found for {name}")
    short_num = 0
    display = []
    for b in rows:
        if b["Resource"] == "Short":
            short_num += 1
            label = f"Short {short_num}"
        elif b["Resource"] == "Meal":
            label = "Meal"
        elif b["Resource"] == "WB20":
            label = "WB20"
        else:
            label = "WB70"
        display.append({
            "Break": label,
            "Resource": b["Resource"],
            "Start": b["Start"],
            "Finish": b["Finish"],
            "Bar_Text": f"<b>{fmt(b['Start'])}-{fmt(b['Finish'])}</b>",
        })
    df = pd.DataFrame(display)
    color_map = {
        "Short": "#3b82f6", "Meal": "#f97316", "WB20": "#22c55e", "WB70": "#a855f7",
    }
    fig = px.timeline(
        df, x_start="Start", x_end="Finish", y="Break", color="Resource", text="Bar_Text",
        color_discrete_map=color_map,
    )
    fig.update_traces(
        textposition="inside", insidetextanchor="middle", textangle=0,
        textfont=dict(family="Montserrat, sans-serif", color="white", size=13),
        marker=dict(line=dict(width=1, color="rgba(255, 255, 255, 0.6)")),
    )
    fig.update_yaxes(categoryorder="array", categoryarray=[r["Break"] for r in display])
    fig.update_layout(
        title=dict(
            text=(
                f"<b>{name} — Break Timetable</b><br>"
                f"<span style='font-size:14px'>Date: {payload['date']} | "
                f"Shift: {payload['shift_label']} | Power Unit: {payload['pu']} | "
                f"{fmt(payload['shift_start_dt'])}–{fmt(payload['shift_end_dt'])}</span>"
            ),
            x=0.5, xanchor="center", y=0.94, yanchor="top",
            font=dict(family="Montserrat, sans-serif", color="black", size=22),
        ),
        plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="Montserrat, sans-serif", color="black", size=12),
        xaxis=dict(
            showgrid=True, gridcolor="#e5e5e5", tickformat="%H:%M", dtick=3600000,
            title="<b>Time</b>", side="bottom",
            range=[payload["shift_start_dt"], payload["shift_end_dt"]],
        ),
        yaxis=dict(
            showgrid=True, gridcolor="#f3f4f6", title="", autorange="reversed",
            tickfont=dict(color="#1c2838", size=12, family="Montserrat, sans-serif"),
        ),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.005,
            xanchor="center", x=0.5, title="",
        ),
        margin=dict(l=0, r=0, t=125, b=40),
        height=560,
    )
    return fig


def generate_individual_card(payload, name):
    return _render_png(_individual_figure(payload, name), width=1800, height=560)


def generate_cards_zip(payload):
    buffer = BytesIO()
    names = sorted({b["Name"] for b in payload["schedule"]}, key=str.casefold)
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for i, name in enumerate(names, 1):
            archive.writestr(
                f"Individual_Breaks/{i:03d}_{slug(name)}.png",
                generate_individual_card(payload, name),
            )
        archive.writestr(
            "Moderator_break_summaries.txt",
            "\n\n".join(moderator_message(payload, n) for n in names).encode("utf-8"),
        )
    return buffer.getvalue()


def generate_csv(payload):
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "PU", "Shift", "Moderator", "Break type", "Start", "End", "Duration minutes", "Ticket moderator"])
    for b in sorted(payload["schedule"], key=lambda x: (x["Name"].casefold(), x["Start"])):
        writer.writerow([
            payload["date"], payload["pu"], payload["shift_label"], b["Name"], b["Resource"],
            fmt(b["Start"]), fmt(b["Finish"]),
            int((b["Finish"] - b["Start"]).total_seconds() / 60),
            bool(b.get("TicketModerator")),
        ])
    return buffer.getvalue().encode("utf-8-sig")
