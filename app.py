import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import numpy as np
import scipy.sparse as sp
from scipy.optimize import milp, LinearConstraint, Bounds
from datetime import datetime, timedelta
from itertools import permutations
import random
import math
import hashlib
import sqlite3
import json
import os
import re
import calendar as pycalendar
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. PAGE CONFIGURATION, NAVIGATION & CONSTANTS
# ==========================================
st.set_page_config(page_title="Lark Break Planner", layout="wide")

SHIFT_PRESETS = {
    "Morning": {
        "shift_start": "07:30",
        "shift_end": "16:30",
        "earliest_break": "08:30",
        "final_break": "15:45",
        "meal_start": "12:00",
        "meal_end": "14:30",
    },
    "Mid": {
        "shift_start": "15:00",
        "shift_end": "00:00",
        "earliest_break": "16:00",
        "final_break": "23:15",
        "meal_start": "17:00",
        "meal_end": "21:00",
    },
    "Night": {
        "shift_start": "23:30",
        "shift_end": "08:00",
        "earliest_break": "00:30",
        "final_break": "07:15",
        "meal_start": "02:00",
        "meal_end": "05:00",
    },
}

BREAK_TYPES = ["Short", "Meal", "WB20", "WB70"]
TIME_STEP = 5
MAX_PATTERNS_PER_PROFILE = 300
SOLVER_TIME_LIMIT = 60
MIP_REL_GAP = 0.05
BASE_VOLUME = 1836.0
OVERLAP_COVERAGE_FACTOR = 0.50
TURKEY_TZ = ZoneInfo("Europe/Istanbul")
GOOGLE_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SCHEDULES_SHEET_NAME = "Schedules"
BREAKS_SHEET_NAME = "Breaks"
LEGACY_DB_PATH = os.environ.get(
    "BREAK_PLANNER_DB_PATH",
    str(Path.cwd() / "break_schedules.db"),
)

SCHEDULE_HEADERS = [
    "schedule_id", "schedule_date", "shift_name", "power_unit", "revision", "is_current",
    "uploader", "uploaded_at", "restored_from_revision", "shift_start", "shift_end",
    "earliest_iso", "final_iso", "peak_concurrent", "peak_wb70", "ticket_moderator_count",
]
BREAK_HEADERS = [
    "schedule_id", "moderator", "ticket_moderator", "break_type", "start_iso", "finish_iso", "bar_text"
]

st.sidebar.title("Navigation")
page = st.sidebar.radio(
    "Page",
    ["Break Planner", "Schedule Calendar", "Storage & Setup"],
    index=0,
)

if page == "Break Planner":
    st.title("Shift Break Optimizer")
    st.markdown(
        "Maximize on-duty staff while strictly enforcing meal windows, shift limits, "
        "inside-time rules, fixed WB70 times, per-moderator WB70 durations, moderator break entitlements, "
        "ticket coverage, and queue-pressure-aware break placement. Meal breaks are never allowed to be the first break, "
        "and non-fixed WB70s can optionally be restricted to the first half of the shift."
    )

    # ==========================================
    # 2. SIDEBAR RULES & CONFIGURATION
    # ==========================================
    st.sidebar.markdown("---")
    st.sidebar.header("Shift Rules")
    shift_preset = st.sidebar.selectbox(
        "Shift Rule Preset",
        ["Morning", "Mid", "Night", "Custom"],
        index=0,
        help="Morning, Mid and Night automatically load the standard shift rules. Custom unlocks all shift-specific fields.",
    )

    if shift_preset == "Custom":
        custom_defaults = SHIFT_PRESETS["Morning"]
        shift_start_str = st.sidebar.text_input(
            "Shift Start", value=custom_defaults["shift_start"], key="custom_shift_start"
        )
        shift_end_str = st.sidebar.text_input(
            "Shift End", value=custom_defaults["shift_end"], key="custom_shift_end"
        )
        earliest_break_str = st.sidebar.text_input(
            "Earliest Break Allowed", value=custom_defaults["earliest_break"], key="custom_earliest_break"
        )
        final_break_str = st.sidebar.text_input(
            "Final Break Must End By", value=custom_defaults["final_break"], key="custom_final_break"
        )
        st.sidebar.markdown("---")
        meal_start_str = st.sidebar.text_input(
            "Meal Window Start", value=custom_defaults["meal_start"], key="custom_meal_start"
        )
        meal_end_str = st.sidebar.text_input(
            "Meal Window End", value=custom_defaults["meal_end"], key="custom_meal_end"
        )
    else:
        preset_rules = SHIFT_PRESETS[shift_preset]
        shift_start_str = st.sidebar.text_input(
            "Shift Start", value=preset_rules["shift_start"], disabled=True, key=f"{shift_preset}_shift_start"
        )
        shift_end_str = st.sidebar.text_input(
            "Shift End", value=preset_rules["shift_end"], disabled=True, key=f"{shift_preset}_shift_end"
        )
        earliest_break_str = st.sidebar.text_input(
            "Earliest Break Allowed", value=preset_rules["earliest_break"], disabled=True, key=f"{shift_preset}_earliest_break"
        )
        final_break_str = st.sidebar.text_input(
            "Final Break Must End By", value=preset_rules["final_break"], disabled=True, key=f"{shift_preset}_final_break"
        )
        st.sidebar.markdown("---")
        meal_start_str = st.sidebar.text_input(
            "Meal Window Start", value=preset_rules["meal_start"], disabled=True, key=f"{shift_preset}_meal_start"
        )
        meal_end_str = st.sidebar.text_input(
            "Meal Window End", value=preset_rules["meal_end"], disabled=True, key=f"{shift_preset}_meal_end"
        )

    st.sidebar.markdown("---")
    st.sidebar.subheader("Universal Rules")
    min_gap = int(st.sidebar.number_input("Minimum Inside Time (mins)", value=45, step=5))
    max_gap = int(st.sidebar.number_input("Maximum Inside Time (mins)", value=105, step=5))

    st.sidebar.subheader("Break Durations (mins)")
    dur_short = int(st.sidebar.number_input("Short Break", value=15, step=5))
    dur_meal = int(st.sidebar.number_input("Meal Break", value=30, step=5))
    dur_wb20 = int(st.sidebar.number_input("WB20 Break", value=20, step=5))
    dur_wb70 = int(st.sidebar.number_input("WB70 Break", value=70, step=5))

    st.sidebar.subheader("WB70 Placement")
    allow_wb70_second_half = st.sidebar.toggle(
        "Allow WB70s in second half of shift",
        value=False,
        help=(
            "When off, every non-fixed WB70 must finish by the exact midpoint of the shift. "
            "A moderator with a Fixed WB70 Start always follows that fixed time, even if it falls in or extends into the second half."
        ),
    )
    if allow_wb70_second_half:
        st.sidebar.caption("WB70 placement: second-half WB70s are allowed.")
    else:
        st.sidebar.caption(
            "WB70 placement: non-fixed WB70s must finish by the shift midpoint. Fixed WB70 times override this rule."
        )

    if shift_preset in ("Morning", "Mid"):
        st.sidebar.caption(
            "Pressure model: base volume 1836. The 15:00–16:30 Morning/Mid overlap is treated as two-shift coverage (50% relative pressure)."
        )
    elif shift_preset == "Night":
        st.sidebar.caption(
            "Pressure model: Night volume is treated as uniform across the shift, so no Night hour receives a volume-based preference."
        )
    else:
        st.sidebar.caption(
            "Pressure model: Custom schedules use uniform relative pressure because no preset-specific volume/overlap profile is selected."
        )

    DURATIONS = {
        "Short": dur_short,
        "Meal": dur_meal,
        "WB20": dur_wb20,
        "WB70": dur_wb70,
    }
elif page == "Schedule Calendar":
    st.title("Saved Break Schedule Calendar")
    st.markdown(
        "Choose a date to view the current prepared break schedules for each Shift and Power Unit. "
        "Every save is retained as a revision, so an older version can be reviewed or restored without data loss."
    )
else:
    st.title("Storage & Setup")
    st.markdown(
        "Schedule storage uses a private Google Sheet instead of Streamlit's temporary local disk. "
        "This page checks the connection and provides a one-time migration option for legacy SQLite schedules."
    )

# ==========================================
# 3. HELPER FUNCTIONS
# ==========================================
def parse_time(time_str, base_date=datetime(2026, 1, 1)):
    """Convert HH:MM to datetime without automatic overnight assumptions."""
    if time_str is None or pd.isna(time_str) or str(time_str).strip() == "":
        return None
    try:
        h, m = map(int, str(time_str).strip().split(":"))
        return base_date.replace(hour=h, minute=m, second=0, microsecond=0)
    except Exception:
        return None


def safe_nonnegative_int(value):
    if pd.isna(value) or value == "":
        return 0
    try:
        return max(0, int(value))
    except Exception:
        return 0


def safe_bool(value):
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y", "x", "checked"}


def ceil_step(value, step=TIME_STEP):
    return int(math.ceil(value / step) * step)


def floor_step(value, step=TIME_STEP):
    return int(math.floor(value / step) * step)


def unique_break_orders(counts):
    """Return arbitrary unique type orders, with Meal forbidden in first position."""
    items = []
    for b_type in BREAK_TYPES:
        items.extend([b_type] * counts.get(b_type, 0))

    # Meal is a hard first-position exclusion even for WB70 Meal Exception moderators.
    if len(items) <= 8:
        return [
            order for order in set(permutations(items))
            if order and order[0] != "Meal"
        ]

    rng = random.Random(81731)
    seen = set()
    base = list(items)
    for _ in range(1200):
        rng.shuffle(base)
        order = tuple(base)
        if order and order[0] != "Meal":
            seen.add(order)
    return list(seen)


def profile_seed(profile_key):
    raw = repr(profile_key).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


def build_candidate_patterns(
    counts,
    durations,
    total_shift_mins,
    earliest_mins,
    final_mins,
    meal_start_mins,
    meal_end_mins,
    min_inside,
    max_inside,
    fixed_wb70_mins,
    allow_wb70_second_half,
    max_patterns=MAX_PATTERNS_PER_PROFILE,
):
    """
    Generate complete individually-feasible schedules first.

    Each candidate already satisfies:
      - exact entitlements
      - arbitrary break-type order, with Meal never first
      - earliest/final break limits
      - min/max inside time before, between and after breaks
      - normal Meal Window unless WB70 exists
      - exact Fixed WB70 start when provided
      - optional first-half-only WB70 placement for non-fixed WB70s
      - fixed WB70 times override the first-half-only setting
      - 5-minute break-start grid

    The global optimizer then only chooses WHICH valid candidate each moderator uses.
    This avoids the huge moderator x position x type x time binary formulation.
    """
    total_breaks = sum(counts.values())
    if total_breaks <= 0:
        return []

    orders = unique_break_orders(counts)
    if not orders:
        return []

    meal_exception = counts.get("WB70", 0) > 0

    profile_key = (
        tuple((b, counts.get(b, 0)) for b in BREAK_TYPES),
        tuple((b, durations[b]) for b in BREAK_TYPES),
        total_shift_mins,
        earliest_mins,
        final_mins,
        meal_start_mins,
        meal_end_mins,
        min_inside,
        max_inside,
        fixed_wb70_mins,
        bool(allow_wb70_second_half),
    )
    rng = random.Random(profile_seed(profile_key))
    rng.shuffle(orders)

    patterns = {}
    max_passes = 14
    pass_no = 0

    # Give each order several opportunities so the candidate pool is not biased
    # toward one specific break-type sequence.
    while len(patterns) < max_patterns and pass_no < max_passes:
        pass_no += 1
        rng.shuffle(orders)
        per_order_target = max(2, math.ceil(max_patterns / max(1, len(orders))))

        for order in orders:
            if len(patterns) >= max_patterns:
                break

            collected_before = len(patterns)

            def recurse(position, starts):
                if len(patterns) >= max_patterns:
                    return
                if len(patterns) - collected_before >= per_order_target:
                    return

                b_type = order[position]
                dur = durations[b_type]

                if position == 0:
                    low = max(earliest_mins, min_inside)
                    high = max_inside
                else:
                    prev_type = order[position - 1]
                    prev_end = starts[-1] + durations[prev_type]
                    low = prev_end + min_inside
                    high = prev_end + max_inside

                low = ceil_step(low)
                high = floor_step(min(high, final_mins - dur, total_shift_mins - dur))

                # Minimum room needed after the current break for all remaining
                # breaks plus the final inside-time segment.
                remaining_types = order[position + 1 :]
                minimum_after = (
                    sum(durations[x] for x in remaining_types)
                    + min_inside * (len(remaining_types) + 1)
                )
                high = min(high, floor_step(total_shift_mins - dur - minimum_after))

                if high < low:
                    return

                if b_type == "WB70" and fixed_wb70_mins is not None:
                    # Fixed WB70 always overrides the first-half-only setting.
                    if (
                        fixed_wb70_mins < low
                        or fixed_wb70_mins > high
                        or fixed_wb70_mins % TIME_STEP != 0
                    ):
                        candidate_starts = []
                    else:
                        candidate_starts = [fixed_wb70_mins]
                else:
                    if b_type == "WB70" and not allow_wb70_second_half:
                        # "Not allowed in the second half" means the entire non-fixed
                        # WB70 must be complete by the exact midpoint of the shift.
                        shift_midpoint = total_shift_mins / 2.0
                        latest_first_half_start = floor_step(shift_midpoint - dur)
                        high = min(high, latest_first_half_start)
                        if high < low:
                            return
                    candidate_starts = list(range(low, high + 1, TIME_STEP))
                    rng.shuffle(candidate_starts)

                for start in candidate_starts:
                    if b_type == "Meal" and not meal_exception:
                        if start < meal_start_mins or start + dur > meal_end_mins:
                            continue

                    if position == len(order) - 1:
                        end = start + dur
                        final_inside = total_shift_mins - end
                        if end > final_mins:
                            continue
                        if not (min_inside <= final_inside <= max_inside):
                            continue

                        full_starts = tuple(starts + [start])
                        key = tuple(zip(order, full_starts))
                        patterns[key] = {
                            "Order": tuple(order),
                            "Starts": full_starts,
                        }
                    else:
                        recurse(position + 1, starts + [start])

                    if len(patterns) >= max_patterns:
                        return
                    if len(patterns) - collected_before >= per_order_target:
                        return

            recurse(0, [])

    return list(patterns.values())


def clock_minutes(dt):
    return dt.hour * 60 + dt.minute


def build_pressure_profile(preset_name, shift_start_dt, timeline_mins):
    """
    Build a relative operational-pressure weight for every 5-minute point.

    Morning/Mid:
      - underlying volume is constant at 1836
      - 15:00–16:30 is treated as two-shift coverage, so relative pressure = 0.50

    Night:
      - volume is treated as uniform across the entire shift
      - relative pressure = 1.00 at every 5-minute point
      - no Night hour is preferred or penalized based on volume

    Custom:
      - neutral uniform pressure = 1.00
    """
    raw_volume = []
    effective_pressure = []
    pressure_weights = []
    coverage_labels = []

    overlap_start = 15 * 60
    overlap_end = 16 * 60 + 30

    for t in timeline_mins:
        dt = shift_start_dt + timedelta(minutes=int(t))
        minute_of_day = clock_minutes(dt)

        volume = BASE_VOLUME
        coverage_factor = 1.0
        label = "Standard"

        if preset_name == "Night":
            # Night output is now uniform across hours. Keep a flat relative
            # pressure of 1.00 so the optimizer does not prefer one Night hour
            # over another based on volume.
            volume = BASE_VOLUME
            label = "Uniform Night pressure"
        elif preset_name in ("Morning", "Mid"):
            if overlap_start <= minute_of_day < overlap_end:
                coverage_factor = OVERLAP_COVERAGE_FACTOR
                label = "Morning/Mid overlap"
        elif preset_name == "Custom":
            label = "Uniform custom pressure"

        effective = volume * coverage_factor
        weight = effective / BASE_VOLUME

        raw_volume.append(volume)
        effective_pressure.append(effective)
        pressure_weights.append(weight)
        coverage_labels.append(label)

    return {
        "RawVolume": np.array(raw_volume, dtype=float),
        "EffectivePressure": np.array(effective_pressure, dtype=float),
        "Weight": np.array(pressure_weights, dtype=float),
        "Label": coverage_labels,
    }



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


def safe_filename_component(value):
    """Convert user-entered metadata into a filesystem-safe filename component."""
    value = str(value or "").strip()
    value = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "NA"


def build_export_filename(prefix, schedule_date, shift_name, power_unit, extension="png"):
    date_part = (
        schedule_date.strftime("%Y-%m-%d")
        if hasattr(schedule_date, "strftime")
        else safe_filename_component(schedule_date)
    )
    return (
        f"{safe_filename_component(prefix)}_"
        f"{safe_filename_component(date_part)}_"
        f"{safe_filename_component(shift_name)}_"
        f"{safe_filename_component(power_unit)}.{extension}"
    )


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

    num_moderators = len(sched_df["Task"].unique())
    dynamic_height = max(650, num_moderators * 45 + 210)
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
            tickfont=dict(color="#1c2838", size=12, family="Montserrat, sans-serif"),
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.005,
            xanchor="center",
            x=0.5,
            title="",
        ),
        margin=dict(l=0, r=0, t=125, b=40),
        height=dynamic_height,
    )
    return fig, dynamic_height


def pattern_vectors(pattern, durations, timeline_mins):
    """Return total-break and WB70-only active vectors for one candidate pattern."""
    active = np.zeros(len(timeline_mins), dtype=float)
    active_wb70 = np.zeros(len(timeline_mins), dtype=float)

    for b_type, start in zip(pattern["Order"], pattern["Starts"]):
        finish = start + durations[b_type]
        mask = np.array([(start <= t < finish) for t in timeline_mins], dtype=float)
        active += mask
        if b_type == "WB70":
            active_wb70 += mask

    return active, active_wb70


def greedy_fallback(moderators, pattern_sets, vector_sets, timeline_len, pressure_weights):
    """Produce a complete feasible fallback while honoring the ticket-coverage hard rule."""
    ticket_indices = [i for i, mod in enumerate(moderators) if mod.get("TicketModerator", False)]
    ticket_count = len(ticket_indices)
    rng = random.Random(99173)
    best_solution = None
    best_score = None

    # Multi-start greedy reduces the chance that an early pattern choice blocks the last ticket moderator.
    for attempt in range(30):
        overall = np.zeros(timeline_len, dtype=float)
        wb70 = np.zeros(timeline_len, dtype=float)
        ticket_active = np.zeros(timeline_len, dtype=float)
        chosen = {}

        # Ticket moderators are placed first, then WB70-heavy moderators.
        order = list(range(len(moderators)))
        if attempt == 0:
            order.sort(
                key=lambda i: (
                    1 if moderators[i].get("TicketModerator", False) else 0,
                    moderators[i]["Counts"].get("WB70", 0),
                ),
                reverse=True,
            )
        else:
            rng.shuffle(order)
            order.sort(key=lambda i: 1 if moderators[i].get("TicketModerator", False) else 0, reverse=True)

        failed = False
        for m_idx in order:
            best_idx = None
            local_best_score = None
            active_mat, wb_mat = vector_sets[m_idx]
            candidate_order = list(range(active_mat.shape[1]))
            if attempt:
                rng.shuffle(candidate_order)

            for p_idx in candidate_order:
                candidate_active = active_mat[:, p_idx]
                if moderators[m_idx].get("TicketModerator", False) and ticket_count >= 2:
                    candidate_ticket = ticket_active + candidate_active
                    if np.any(candidate_ticket >= ticket_count - 1e-9):
                        continue

                new_overall = overall + candidate_active
                new_wb = wb70 + wb_mat[:, p_idx]
                weighted_load = pressure_weights * new_overall
                score = (
                    1_000_000 * np.max(new_wb)
                    + 100_000 * np.max(weighted_load)
                    + 30_000 * np.max(new_overall)
                    + np.sum(pressure_weights * (new_overall * (new_overall + 1) / 2))
                )
                if attempt:
                    score += rng.random() * 0.001
                if local_best_score is None or score < local_best_score:
                    local_best_score = score
                    best_idx = p_idx

            if best_idx is None:
                failed = True
                break

            chosen[m_idx] = best_idx
            overall += active_mat[:, best_idx]
            wb70 += wb_mat[:, best_idx]
            if moderators[m_idx].get("TicketModerator", False):
                ticket_active += active_mat[:, best_idx]

        if failed or len(chosen) != len(moderators):
            continue

        if ticket_count >= 2 and np.any(ticket_active >= ticket_count - 1e-9):
            continue

        final_score = (
            1_000_000 * np.max(wb70)
            + 100_000 * np.max(pressure_weights * overall)
            + 30_000 * np.max(overall)
            + np.sum(pressure_weights * (overall * (overall + 1) / 2))
        )
        if best_score is None or final_score < best_score:
            best_score = final_score
            best_solution = (chosen, int(np.max(overall)), int(np.max(wb70)))

    if best_solution is None:
        raise RuntimeError(
            "The fallback scheduler could not find a ticket-safe complete schedule. "
            "Try generating again or widening the break rules."
        )
    return best_solution

def optimize_pattern_selection(moderators, pattern_sets, vector_sets, timeline_mins, pressure_weights):
    """
    Set-partitioning MILP: choose exactly one complete feasible pattern per moderator.

    This model is dramatically smaller than the previous break-level formulation,
    so time limits no longer get confused with individual schedule infeasibility.
    """
    moderator_count = len(moderators)
    timeline_len = len(timeline_mins)
    ticket_count = sum(1 for mod in moderators if mod.get("TicketModerator", False))

    # One binary variable for every moderator/candidate-pattern pair.
    y_offsets = []
    cursor = 0
    for patterns in pattern_sets:
        y_offsets.append(cursor)
        cursor += len(patterns)
    n_y = cursor

    idx_max_concurrent = n_y
    idx_max_wb70 = n_y + 1
    idx_max_weighted = n_y + 2
    idx_e = n_y + 3
    n_e = timeline_len * moderator_count
    n_vars = n_y + 3 + n_e

    c = np.zeros(n_vars, dtype=float)
    # Priority hierarchy after all hard constraints:
    #   1. WB70 peak overlap
    #   2. worst pressure-weighted concurrent break load
    #   3. absolute peak concurrency
    #   4. pressure-weighted triangular smoothing
    c[idx_max_wb70] = 1_000_000.0
    c[idx_max_weighted] = 100_000.0
    c[idx_max_concurrent] = 30_000.0

    for t_idx in range(timeline_len):
        for k in range(moderator_count):
            c[idx_e + t_idx * moderator_count + k] = float(k + 1) * float(pressure_weights[t_idx])

    integrality = np.zeros(n_vars, dtype=int)
    integrality[:n_y] = 1
    integrality[idx_max_concurrent] = 1
    integrality[idx_max_wb70] = 1

    lower = np.zeros(n_vars, dtype=float)
    upper = np.full(n_vars, np.inf, dtype=float)
    upper[:n_y] = 1.0
    upper[idx_max_concurrent] = float(moderator_count)
    upper[idx_max_wb70] = float(moderator_count)
    upper[idx_max_weighted] = float(moderator_count)
    upper[idx_e:] = 1.0

    rows = []
    lbs = []
    ubs = []

    # Exactly one complete feasible candidate per moderator.
    for m_idx, patterns in enumerate(pattern_sets):
        row = {}
        off = y_offsets[m_idx]
        for p_idx in range(len(patterns)):
            row[off + p_idx] = 1.0
        rows.append(row)
        lbs.append(1.0)
        ubs.append(1.0)

    # Concurrency and flattening constraints.
    for t_idx in range(timeline_len):
        load_terms = {}
        wb_terms = {}
        ticket_terms = {}

        for m_idx in range(moderator_count):
            active_mat, wb_mat = vector_sets[m_idx]
            off = y_offsets[m_idx]
            for p_idx in range(active_mat.shape[1]):
                a = active_mat[t_idx, p_idx]
                w = wb_mat[t_idx, p_idx]
                if a:
                    load_terms[off + p_idx] = float(a)
                    if moderators[m_idx].get("TicketModerator", False):
                        ticket_terms[off + p_idx] = float(a)
                if w:
                    wb_terms[off + p_idx] = float(w)

        row = dict(load_terms)
        row[idx_max_concurrent] = -1.0
        rows.append(row)
        lbs.append(-np.inf)
        ubs.append(0.0)

        # Pressure-weighted peak. A break during a 0.50-pressure overlap period
        # costs half as much as the same break during a normal 1.00-pressure period.
        row = {var_idx: value * float(pressure_weights[t_idx]) for var_idx, value in load_terms.items()}
        row[idx_max_weighted] = -1.0
        rows.append(row)
        lbs.append(-np.inf)
        ubs.append(0.0)

        row = dict(wb_terms)
        row[idx_max_wb70] = -1.0
        rows.append(row)
        lbs.append(-np.inf)
        ubs.append(0.0)

        # Hard ticket-coverage rule: at every 5-minute point, at least one designated
        # ticket moderator must remain on duty.
        if ticket_count >= 2:
            rows.append(dict(ticket_terms))
            lbs.append(-np.inf)
            ubs.append(float(ticket_count - 1))

        row = dict(load_terms)
        for k in range(moderator_count):
            row[idx_e + t_idx * moderator_count + k] = -1.0
        rows.append(row)
        lbs.append(0.0)
        ubs.append(0.0)

    data = []
    row_idx = []
    col_idx = []
    for r, row in enumerate(rows):
        for c_idx, value in row.items():
            row_idx.append(r)
            col_idx.append(c_idx)
            data.append(value)

    A = sp.csr_matrix((data, (row_idx, col_idx)), shape=(len(rows), n_vars))
    constraints = LinearConstraint(A, np.array(lbs), np.array(ubs))
    bounds = Bounds(lower, upper)

    result = milp(
        c,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
        options={
            "time_limit": SOLVER_TIME_LIMIT,
            "mip_rel_gap": MIP_REL_GAP,
            "disp": False,
        },
    )

    if result.x is None:
        chosen, peak, wb_peak = greedy_fallback(
            moderators, pattern_sets, vector_sets, timeline_len, pressure_weights
        )
        return {
            "Chosen": chosen,
            "Peak": peak,
            "WB70Peak": wb_peak,
            "WeightedPeak": float(np.max(pressure_weights * np.sum(
                [vector_sets[m_idx][0][:, chosen[m_idx]] for m_idx in range(len(moderators))], axis=0
            ))),
            "UsedFallback": True,
            "SolverMessage": result.message,
        }

    chosen = {}
    for m_idx, patterns in enumerate(pattern_sets):
        off = y_offsets[m_idx]
        vals = result.x[off : off + len(patterns)]
        chosen[m_idx] = int(np.argmax(vals))

    return {
        "Chosen": chosen,
        "Peak": int(round(result.x[idx_max_concurrent])),
        "WB70Peak": int(round(result.x[idx_max_wb70])),
        "WeightedPeak": float(result.x[idx_max_weighted]),
        "UsedFallback": not bool(result.success),
        "SolverMessage": result.message,
    }


# ==========================================
# 4. GOOGLE SHEETS STORAGE, REVISION HISTORY & MIGRATION
# ==========================================
def _service_account_info():
    """Read Google service-account credentials from Streamlit Secrets or an environment JSON blob."""
    env_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if env_json:
        info = json.loads(env_json)
    else:
        try:
            info = dict(st.secrets["gcp_service_account"])
        except Exception as exc:
            raise RuntimeError(
                "Google Sheets credentials are not configured. Add [gcp_service_account] to Streamlit Secrets."
            ) from exc
    if "private_key" in info and isinstance(info["private_key"], str):
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    return info


def get_spreadsheet_id():
    env_id = os.environ.get("BREAK_PLANNER_SPREADSHEET_ID", "").strip()
    if env_id:
        return env_id
    try:
        value = str(st.secrets["break_planner"]["spreadsheet_id"]).strip()
        if value:
            return value
    except Exception:
        pass
    raise RuntimeError(
        "Google Sheets spreadsheet_id is not configured. Add [break_planner] spreadsheet_id to Streamlit Secrets."
    )


@st.cache_resource(show_spinner=False)
def get_gspread_client():
    credentials = Credentials.from_service_account_info(
        _service_account_info(), scopes=GOOGLE_SHEETS_SCOPES
    )
    return gspread.authorize(credentials)


@st.cache_resource(show_spinner=False)
def get_break_planner_spreadsheet():
    return get_gspread_client().open_by_key(get_spreadsheet_id())


def _get_or_create_worksheet(spreadsheet, title, headers, rows=2000):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=max(20, len(headers) + 2))
        ws.append_row(headers, value_input_option="RAW")
        return ws

    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(headers, value_input_option="RAW")
    elif first_row[: len(headers)] != headers:
        raise RuntimeError(
            f"Worksheet '{title}' has unexpected headers. Expected: {', '.join(headers)}"
        )
    return ws


def get_storage_worksheets():
    spreadsheet = get_break_planner_spreadsheet()
    schedules_ws = _get_or_create_worksheet(spreadsheet, SCHEDULES_SHEET_NAME, SCHEDULE_HEADERS)
    breaks_ws = _get_or_create_worksheet(spreadsheet, BREAKS_SHEET_NAME, BREAK_HEADERS, rows=10000)
    return spreadsheet, schedules_ws, breaks_ws


def storage_connection_status():
    spreadsheet, schedules_ws, breaks_ws = get_storage_worksheets()
    return {
        "title": spreadsheet.title,
        "spreadsheet_id": get_spreadsheet_id(),
        "schedules_rows": max(0, schedules_ws.row_count - 1),
        "breaks_rows": max(0, breaks_ws.row_count - 1),
    }


def _records(ws):
    return ws.get_all_records(default_blank="")


def _mark_previous_current_false(schedules_ws, records, schedule_date, shift_name, power_unit):
    current_col = SCHEDULE_HEADERS.index("is_current") + 1
    for row_number, row in enumerate(records, start=2):
        if (
            str(row.get("schedule_date", "")) == schedule_date
            and str(row.get("shift_name", "")).strip() == shift_name
            and str(row.get("power_unit", "")).strip() == power_unit
            and safe_bool(row.get("is_current", False))
        ):
            schedules_ws.update_cell(row_number, current_col, "FALSE")


def save_schedule_record(
    schedule_date,
    shift_name,
    power_unit,
    uploader,
    shift_start,
    shift_end,
    earliest_dt,
    final_dt,
    schedule,
    peak_concurrent,
    peak_wb70,
    restored_from_revision="",
    uploaded_at_override=None,
):
    """Append a new immutable revision and mark it as current for Date + Shift + PU."""
    _, schedules_ws, breaks_ws = get_storage_worksheets()
    existing = _records(schedules_ws)

    date_text = schedule_date.strftime("%Y-%m-%d") if hasattr(schedule_date, "strftime") else str(schedule_date)
    shift_name = str(shift_name).strip()
    power_unit = str(power_unit).strip()
    uploader = str(uploader or "").strip()

    matching = [
        r for r in existing
        if str(r.get("schedule_date", "")) == date_text
        and str(r.get("shift_name", "")).strip() == shift_name
        and str(r.get("power_unit", "")).strip() == power_unit
    ]
    revisions = []
    for r in matching:
        try:
            revisions.append(int(float(r.get("revision", 0) or 0)))
        except Exception:
            pass
    revision = (max(revisions) if revisions else 0) + 1
    schedule_id = uuid.uuid4().hex
    uploaded_at = uploaded_at_override or datetime.now(TURKEY_TZ).isoformat(timespec="seconds")
    ticket_names = {
        item.get("Name") or re.sub(r"<[^>]+>", "", str(item.get("Task", "")))
        for item in schedule
        if safe_bool(item.get("TicketModerator", False))
    }

    # Write break rows first. If the final schedule-row append fails, these rows are harmless orphans
    # because Calendar records are discovered from the Schedules tab only.
    break_rows = []
    for item in sorted(schedule, key=lambda x: (x.get("Name", ""), x["Start"])):
        name = item.get("Name") or re.sub(r"<[^>]+>", "", str(item.get("Task", "")))
        break_rows.append([
            schedule_id,
            name,
            "TRUE" if safe_bool(item.get("TicketModerator", False)) else "FALSE",
            item["Resource"],
            item["Start"].isoformat(),
            item["Finish"].isoformat(),
            item.get("Bar_Text", ""),
        ])
    if break_rows:
        breaks_ws.append_rows(break_rows, value_input_option="RAW")

    schedules_ws.append_row([
        schedule_id,
        date_text,
        shift_name,
        power_unit,
        revision,
        "TRUE",
        uploader,
        uploaded_at,
        restored_from_revision,
        shift_start,
        shift_end,
        earliest_dt.isoformat(),
        final_dt.isoformat(),
        int(peak_concurrent),
        int(peak_wb70),
        len(ticket_names),
    ], value_input_option="RAW")

    # Only after the new revision exists do we retire the previous current revision(s).
    _mark_previous_current_false(schedules_ws, existing, date_text, shift_name, power_unit)
    return uploaded_at, revision, bool(matching)


def load_all_schedule_records():
    _, schedules_ws, _ = get_storage_worksheets()
    return _records(schedules_ws)


def _revision_number(record):
    try:
        return int(float(record.get("revision", 0) or 0))
    except Exception:
        return 0


def load_schedules_for_date(selected_date):
    date_text = selected_date.strftime("%Y-%m-%d") if hasattr(selected_date, "strftime") else str(selected_date)
    records = [r for r in load_all_schedule_records() if str(r.get("schedule_date", "")) == date_text]

    # Prefer explicitly-current records. If an interrupted update ever leaves two current rows,
    # keep only the highest revision for each Date + Shift + PU.
    grouped = {}
    for record in records:
        if not safe_bool(record.get("is_current", False)):
            continue
        key = (record.get("schedule_date", ""), record.get("shift_name", ""), record.get("power_unit", ""))
        if key not in grouped or _revision_number(record) > _revision_number(grouped[key]):
            grouped[key] = record

    shift_order = {"Morning": 0, "Mid": 1, "Night": 2}
    current = list(grouped.values())
    current.sort(key=lambda r: (
        shift_order.get(str(r.get("shift_name", "")), 9),
        str(r.get("shift_name", "")).lower(),
        str(r.get("power_unit", "")).lower(),
    ))
    return current


def load_schedule_counts_for_month(year, month):
    prefix = f"{year:04d}-{month:02d}-"
    current = [r for r in load_all_schedule_records() if safe_bool(r.get("is_current", False))]
    unique = {}
    for r in current:
        if not str(r.get("schedule_date", "")).startswith(prefix):
            continue
        key = (r.get("schedule_date", ""), r.get("shift_name", ""), r.get("power_unit", ""))
        if key not in unique or _revision_number(r) > _revision_number(unique[key]):
            unique[key] = r
    counts = {}
    for r in unique.values():
        date_text = str(r.get("schedule_date", ""))
        counts[date_text] = counts.get(date_text, 0) + 1
    return counts


def load_break_rows_for_schedule_ids(schedule_ids):
    ids = {str(x) for x in schedule_ids}
    if not ids:
        return {}
    _, _, breaks_ws = get_storage_worksheets()
    all_rows = _records(breaks_ws)
    grouped = {sid: [] for sid in ids}
    for row in all_rows:
        sid = str(row.get("schedule_id", ""))
        if sid in ids:
            grouped.setdefault(sid, []).append(row)
    return grouped


def schedule_from_break_rows(rows):
    schedule = []
    for item in rows:
        start_dt = datetime.fromisoformat(str(item["start_iso"]))
        finish_dt = datetime.fromisoformat(str(item["finish_iso"]))
        name = str(item.get("moderator", ""))
        bar_text = str(item.get("bar_text", "")) or f"<b>{start_dt.strftime('%H:%M')}-{finish_dt.strftime('%H:%M')}</b>"
        schedule.append({
            "Name": name,
            "Task": f"<b>{name}</b>",
            "Resource": str(item.get("break_type", "")),
            "Start": start_dt,
            "Finish": finish_dt,
            "Bar_Text": bar_text,
            "TicketModerator": safe_bool(item.get("ticket_moderator", False)),
        })
    return schedule


def load_schedule_revisions(schedule_date, shift_name, power_unit):
    date_text = schedule_date.strftime("%Y-%m-%d") if hasattr(schedule_date, "strftime") else str(schedule_date)
    revisions = [
        r for r in load_all_schedule_records()
        if str(r.get("schedule_date", "")) == date_text
        and str(r.get("shift_name", "")).strip() == str(shift_name).strip()
        and str(r.get("power_unit", "")).strip() == str(power_unit).strip()
    ]
    revisions.sort(key=_revision_number, reverse=True)
    return revisions


def restore_schedule_revision(record, schedule, restored_by):
    saved_date = datetime.strptime(str(record["schedule_date"]), "%Y-%m-%d").date()
    earliest_dt = datetime.fromisoformat(str(record["earliest_iso"]))
    final_dt = datetime.fromisoformat(str(record["final_iso"]))
    return save_schedule_record(
        schedule_date=saved_date,
        shift_name=record["shift_name"],
        power_unit=record["power_unit"],
        uploader=restored_by,
        shift_start=record["shift_start"],
        shift_end=record["shift_end"],
        earliest_dt=earliest_dt,
        final_dt=final_dt,
        schedule=schedule,
        peak_concurrent=int(float(record.get("peak_concurrent", 0) or 0)),
        peak_wb70=int(float(record.get("peak_wb70", 0) or 0)),
        restored_from_revision=f"v{_revision_number(record)}",
    )


def legacy_db_exists():
    return Path(LEGACY_DB_PATH).exists()


def _deserialize_legacy_schedule(schedule_json):
    raw = json.loads(schedule_json)
    schedule = []
    for item in raw:
        start_dt = datetime.fromisoformat(item["Start"])
        finish_dt = datetime.fromisoformat(item["Finish"])
        name = item.get("Name", "")
        schedule.append({
            "Name": name,
            "Task": f"<b>{name}</b>",
            "Resource": item["Resource"],
            "Start": start_dt,
            "Finish": finish_dt,
            "Bar_Text": item.get("Bar_Text", ""),
            "TicketModerator": False,
        })
    return schedule


def migrate_legacy_sqlite_to_sheets():
    """One-time, non-destructive importer for schedules saved by V7/V8 SQLite builds."""
    if not legacy_db_exists():
        return 0, 0
    existing_keys = {
        (str(r.get("schedule_date", "")), str(r.get("shift_name", "")), str(r.get("power_unit", "")))
        for r in load_all_schedule_records()
    }
    migrated = 0
    skipped = 0
    conn = sqlite3.connect(LEGACY_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM saved_schedules ORDER BY schedule_date, shift_name, power_unit").fetchall()
        for row in rows:
            r = dict(row)
            key = (r["schedule_date"], r["shift_name"], r["power_unit"])
            if key in existing_keys:
                skipped += 1
                continue
            schedule = _deserialize_legacy_schedule(r["schedule_json"])
            save_schedule_record(
                schedule_date=datetime.strptime(r["schedule_date"], "%Y-%m-%d").date(),
                shift_name=r["shift_name"],
                power_unit=r["power_unit"],
                uploader=r.get("uploader", ""),
                shift_start=r["shift_start"],
                shift_end=r["shift_end"],
                earliest_dt=datetime.fromisoformat(r["earliest_iso"]),
                final_dt=datetime.fromisoformat(r["final_iso"]),
                schedule=schedule,
                peak_concurrent=r.get("peak_concurrent", 0) or 0,
                peak_wb70=r.get("peak_wb70", 0) or 0,
                restored_from_revision="Legacy SQLite import",
                uploaded_at_override=r.get("uploaded_at") or None,
            )
            existing_keys.add(key)
            migrated += 1
    finally:
        conn.close()
    return migrated, skipped


def schedule_to_display_df(schedule):
    rows = []
    for item in sorted(schedule, key=lambda x: (x.get("Name", ""), x["Start"])):
        rows.append(
            {
                "Moderator": item.get("Name") or re.sub(r"<[^>]+>", "", item.get("Task", "")),
                "Ticket Moderator": "Yes" if safe_bool(item.get("TicketModerator", False)) else "",
                "Break": item["Resource"],
                "Start": item["Start"].strftime("%H:%M"),
                "End": item["Finish"].strftime("%H:%M"),
                "Duration (mins)": int((item["Finish"] - item["Start"]).total_seconds() / 60),
            }
        )
    return pd.DataFrame(rows)



def render_generated_schedule(payload):
    """Render the last generated planner schedule from session state."""
    schedule_date = payload["schedule_date"]
    schedule_shift_name = payload["schedule_shift_name"]
    power_unit = payload["power_unit"]
    shift_start_str = payload["shift_start_str"]
    shift_end_str = payload["shift_end_str"]
    earliest_dt = payload["earliest_dt"]
    final_dt = payload["final_dt"]
    schedule = payload["schedule"]
    sched_df = pd.DataFrame(schedule).sort_values(
        by=["Task", "Start"], ascending=[False, True]
    )
    concurrency_df = payload["concurrency_df"].copy()
    shift_preset = payload["shift_preset"]

    if payload.get("used_fallback"):
        st.warning(
            "⚠️ The mathematical optimizer reached its time/optimality limit, so the app used its "
            "complete feasible fallback selection rather than incorrectly reporting the schedule as impossible."
        )

    st.success(
        f"✅ Current generated schedule ready for review. Peak concurrent breaks: **{payload['peak_concurrent']}**  |  "
        f"Peak concurrent WB70s: **{payload['peak_wb70']}**"
    )
    if payload.get("ticket_moderator_count", 0) >= 2:
        st.caption(
            f"🎫 Ticket coverage protected: {payload['ticket_moderator_count']} designated Ticket Moderators; "
            f"minimum simultaneously on duty during breakable intervals: {payload.get('min_ticket_on_duty', 1)}."
        )

    wb70_midpoint = payload.get("wb70_shift_midpoint")
    if payload.get("allow_wb70_second_half", True):
        st.caption("🧘 WB70 placement: second-half WB70s were allowed for this generated schedule.")
    elif wb70_midpoint is not None:
        st.caption(
            f"🧘 WB70 placement: non-fixed WB70s were required to finish by the shift midpoint "
            f"({wb70_midpoint.strftime('%H:%M')}). Fixed WB70 Start values override this rule."
        )

    if shift_preset in ("Morning", "Mid"):
        st.caption(
            "Pressure-aware optimization active: 15:00–16:30 Morning/Mid overlap is preferred for concurrency because two shifts are covering the queue."
        )
    elif shift_preset == "Night":
        st.caption(
            "Night volume is treated as uniform across the shift, so the optimizer does not favor or avoid any Night hour based on volume. It still minimizes WB70 overlap, peak concurrency, and overall clustering."
        )
    else:
        st.caption(
            "Custom preset uses uniform pressure weighting; optimization still minimizes WB70 overlap and overall concurrency."
        )

    # ==========================================
    # CURRENT GENERATED SCHEDULE VISUALS
    # ==========================================
    st.markdown(
        f"<div style='background-color: #1c2838; color: white; padding: 12px; border-radius: 4px; "
        f"text-align: center; font-size: 22px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
        f"Shift Break Timetable &bull; {schedule_date.strftime('%Y-%m-%d')} &bull; {schedule_shift_name} &bull; {power_unit} &bull; {shift_start_str}-{shift_end_str}</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    fig_gantt, dynamic_height = create_timetable_figure(
        sched_df,
        schedule_date,
        schedule_shift_name,
        power_unit,
        shift_start_str,
        shift_end_str,
    )
    timetable_filename = build_export_filename(
        "Timetable", schedule_date, schedule_shift_name, power_unit
    )

    plotly_config = {
        "toImageButtonOptions": {
            "format": "png",
            "filename": timetable_filename.rsplit(".", 1)[0],
            "height": dynamic_height,
            "width": 1800,
            "scale": 3,
        },
        "displayModeBar": True,
    }

    st.plotly_chart(fig_gantt, use_container_width=True, config=plotly_config)

    try:
        img_bytes = fig_gantt.to_image(
            format="png", width=1800, height=dynamic_height, scale=3
        )
        st.download_button(
            label="📥 Download High-Resolution Timetable (PNG)",
            data=img_bytes,
            file_name=timetable_filename,
            mime="image/png",
            key="current_generated_timetable_download",
        )
    except Exception:
        st.info(
            "💡 To enable the 1-click PNG button, ensure kaleido is installed. "
            "The Plotly toolbar export still remains available."
        )

    st.markdown(
        "<div style='background-color: #1c2838; color: white; padding: 8px; border-radius: 4px; "
        "text-align: center; font-size: 18px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
        "Concurrent Breaks Over Time</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    fig_concurrency = px.area(
        concurrency_df,
        x="Time",
        y="Concurrent Breaks",
        color_discrete_sequence=["#3b82f6"],
    )
    fig_concurrency.update_traces(line_shape="hv", fill="tozeroy", opacity=0.3)
    fig_concurrency.update_layout(
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Montserrat, sans-serif", color="black", size=12),
        xaxis=dict(
            showgrid=True,
            gridcolor="#e5e5e5",
            tickformat="%H:%M",
            dtick=3600000,
            title="<b>Time</b>",
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#f3f4f6",
            title="<b>Staff on Break</b>",
            tickfont=dict(color="#1c2838", size=12, family="Montserrat, sans-serif"),
            dtick=1,
        ),
        margin=dict(l=0, r=0, t=20, b=40),
        height=300,
    )
    st.plotly_chart(fig_concurrency, use_container_width=True)

    # --- Concurrent Break Heatmap ---
    st.markdown(
        "<div style='background-color: #1c2838; color: white; padding: 8px; border-radius: 4px; "
        "text-align: center; font-size: 18px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
        "Concurrent Break Heatmap</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)
    st.caption(
        "Management view: each cell shows how many moderators are simultaneously on break "
        "at that 5-minute point. Lighter pink indicates lower concurrency; deeper violet "
        "indicates higher concurrency. Cells outside the allowed break window are blank."
    )

    fig_heatmap, heatmap_height = create_break_overlap_heatmap(
        schedule,
        earliest_dt,
        final_dt,
        schedule_date=schedule_date,
        shift_name=schedule_shift_name,
        power_unit=power_unit,
    )

    if fig_heatmap is not None:
        heatmap_filename = build_export_filename(
            "Break_Overlap_Heatmap", schedule_date, schedule_shift_name, power_unit
        )
        heatmap_config = {
            "toImageButtonOptions": {
                "format": "png",
                "filename": heatmap_filename.rsplit(".", 1)[0],
                "height": heatmap_height,
                "width": 1800,
                "scale": 3,
            },
            "displayModeBar": True,
        }
        st.plotly_chart(
            fig_heatmap, use_container_width=True, config=heatmap_config
        )

        try:
            heatmap_img_bytes = fig_heatmap.to_image(
                format="png",
                width=1800,
                height=heatmap_height,
                scale=3,
            )
            st.download_button(
                label="📥 Download High-Resolution Break Heatmap (PNG)",
                data=heatmap_img_bytes,
                file_name=heatmap_filename,
                mime="image/png",
                key="current_generated_heatmap_download",
            )
        except Exception:
            st.info(
                "💡 To enable the 1-click heatmap PNG button, ensure kaleido is installed. "
                "The Plotly toolbar export still remains available."
            )
    else:
        st.info("No valid break-window cells were available for the heatmap.")

    st.markdown(
        "<div style='background-color: #1c2838; color: white; padding: 8px; border-radius: 4px; "
        "text-align: center; font-size: 18px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
        "Optimization Pressure Profile</div>",
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    pressure_display_df = concurrency_df.copy()
    pressure_display_df["Relative Pressure"] = pressure_display_df["Pressure Weight"]

    if shift_preset == "Night":
        pressure_hover = {
            "Pressure Source": True,
            "Relative Pressure": ":.3f",
            "Raw Volume": False,
            "Effective Pressure": False,
        }
    else:
        pressure_hover = {
            "Raw Volume": ":.0f",
            "Effective Pressure": ":.0f",
            "Pressure Source": True,
            "Relative Pressure": ":.3f",
        }

    fig_pressure = px.line(
        pressure_display_df,
        x="Time",
        y="Relative Pressure",
        hover_data=pressure_hover,
    )
    fig_pressure.update_layout(
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Montserrat, sans-serif", color="black", size=12),
        xaxis=dict(
            showgrid=True,
            gridcolor="#e5e5e5",
            tickformat="%H:%M",
            dtick=3600000,
            title="<b>Time</b>",
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#f3f4f6",
            title="<b>Relative Queue Pressure</b>",
            rangemode="tozero",
        ),
        margin=dict(l=0, r=0, t=20, b=40),
        height=280,
        showlegend=False,
    )
    st.plotly_chart(fig_pressure, use_container_width=True)

def render_schedule_calendar():
    try:
        get_storage_worksheets()
    except Exception as exc:
        st.error(f"Google Sheets storage is not connected: {exc}")
        st.info("Open the Storage & Setup page for the free Google Sheets setup instructions.")
        return

    today = datetime.now(TURKEY_TZ).date()
    if "calendar_selected_date" not in st.session_state:
        st.session_state.calendar_selected_date = today
    if "calendar_month" not in st.session_state:
        st.session_state.calendar_month = today.replace(day=1)

    month_date = st.session_state.calendar_month
    nav_left, nav_title, nav_right = st.columns([1, 5, 1])
    if nav_left.button("← Previous", use_container_width=True):
        if month_date.month == 1:
            st.session_state.calendar_month = month_date.replace(year=month_date.year - 1, month=12, day=1)
        else:
            st.session_state.calendar_month = month_date.replace(month=month_date.month - 1, day=1)
        st.rerun()
    nav_title.markdown(
        f"<h3 style='text-align:center; margin-top:4px'>{month_date.strftime('%B %Y')}</h3>",
        unsafe_allow_html=True,
    )
    if nav_right.button("Next →", use_container_width=True):
        if month_date.month == 12:
            st.session_state.calendar_month = month_date.replace(year=month_date.year + 1, month=1, day=1)
        else:
            st.session_state.calendar_month = month_date.replace(month=month_date.month + 1, day=1)
        st.rerun()

    counts = load_schedule_counts_for_month(month_date.year, month_date.month)
    weekday_cols = st.columns(7)
    for idx, weekday in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]):
        weekday_cols[idx].markdown(f"**{weekday}**")

    selected_date = st.session_state.calendar_selected_date
    for week in pycalendar.monthcalendar(month_date.year, month_date.month):
        day_cols = st.columns(7)
        for day_idx, day_num in enumerate(week):
            if day_num == 0:
                day_cols[day_idx].markdown("&nbsp;", unsafe_allow_html=True)
                continue
            day_date = month_date.replace(day=day_num)
            count = counts.get(day_date.strftime("%Y-%m-%d"), 0)
            label = f"{day_num}" if count == 0 else f"{day_num} · {count}"
            if day_cols[day_idx].button(
                label,
                key=f"calendar_day_{month_date.year}_{month_date.month}_{day_num}",
                type="primary" if day_date == selected_date else "secondary",
                use_container_width=True,
                help=(f"{count} current schedule(s)" if count else "No saved schedules"),
            ):
                st.session_state.calendar_selected_date = day_date
                selected_date = day_date

    st.markdown(f"### Selected date: {selected_date.strftime('%Y-%m-%d')}")
    records = load_schedules_for_date(selected_date)
    st.caption(
        "Calendar shows the current revision for each Date + Shift + Power Unit. Every previous save is retained in Revision History."
    )

    if not records:
        st.info(f"No saved break schedules for {selected_date.strftime('%Y-%m-%d')}.")
        return

    break_map = load_break_rows_for_schedule_ids([r["schedule_id"] for r in records])
    summary_df = pd.DataFrame([
        {
            "Shift": r["shift_name"],
            "Power Unit": r["power_unit"],
            "Revision": f"v{_revision_number(r)}",
            "Uploaded By": r.get("uploader") or "—",
            "Last Uploaded": datetime.fromisoformat(str(r["uploaded_at"])).strftime("%Y-%m-%d %H:%M"),
            "Ticket Mods": int(float(r.get("ticket_moderator_count", 0) or 0)),
            "Peak Breaks": int(float(r.get("peak_concurrent", 0) or 0)),
            "Peak WB70": int(float(r.get("peak_wb70", 0) or 0)),
        }
        for r in records
    ])
    st.dataframe(summary_df, use_container_width=True, hide_index=True)

    for rec_idx, record in enumerate(records):
        label = f"{record['shift_name']} • {record['power_unit']} • v{_revision_number(record)}"
        with st.expander(label, expanded=(len(records) == 1)):
            uploaded_display = datetime.fromisoformat(str(record["uploaded_at"])).strftime("%Y-%m-%d %H:%M:%S")
            restored_note = f" · **Restored from:** {record.get('restored_from_revision')}" if record.get("restored_from_revision") else ""
            st.markdown(
                f"**Date:** {record['schedule_date']}  ·  **Shift:** {record['shift_name']}  ·  "
                f"**Power Unit:** {record['power_unit']}  ·  **Current revision:** v{_revision_number(record)}  ·  "
                f"**Uploaded by:** {record.get('uploader') or '—'}  ·  **Last uploaded:** {uploaded_display} (Türkiye time)"
                f"{restored_note}"
            )

            schedule = schedule_from_break_rows(break_map.get(str(record["schedule_id"]), []))
            if not schedule:
                st.warning("No break rows were found for this saved revision.")
                continue
            sched_df = pd.DataFrame(schedule).sort_values(by=["Task", "Start"], ascending=[False, True])
            saved_date = datetime.strptime(str(record["schedule_date"]), "%Y-%m-%d").date()

            fig_gantt, dynamic_height = create_timetable_figure(
                sched_df, saved_date, record["shift_name"], record["power_unit"], record["shift_start"], record["shift_end"]
            )
            timetable_filename = build_export_filename("Timetable", saved_date, record["shift_name"], record["power_unit"])
            st.plotly_chart(
                fig_gantt, use_container_width=True, key=f"saved_gantt_{record['schedule_id']}_{rec_idx}",
                config={"toImageButtonOptions": {"format": "png", "filename": timetable_filename.rsplit(".", 1)[0], "height": dynamic_height, "width": 1800, "scale": 3}, "displayModeBar": True},
            )
            try:
                timetable_bytes = fig_gantt.to_image(format="png", width=1800, height=dynamic_height, scale=3)
                st.download_button("📥 Download Timetable PNG", timetable_bytes, file_name=timetable_filename, mime="image/png", key=f"saved_gantt_download_{record['schedule_id']}")
            except Exception:
                st.caption("Install kaleido to enable the one-click PNG download button.")

            with st.expander("View break list"):
                st.dataframe(schedule_to_display_df(schedule), use_container_width=True, hide_index=True)

            earliest_dt = datetime.fromisoformat(str(record["earliest_iso"]))
            final_dt = datetime.fromisoformat(str(record["final_iso"]))
            fig_heatmap, heatmap_height = create_break_overlap_heatmap(
                schedule, earliest_dt, final_dt, schedule_date=saved_date, shift_name=record["shift_name"], power_unit=record["power_unit"]
            )
            if fig_heatmap is not None:
                heatmap_filename = build_export_filename("Break_Overlap_Heatmap", saved_date, record["shift_name"], record["power_unit"])
                st.plotly_chart(
                    fig_heatmap, use_container_width=True, key=f"saved_heatmap_{record['schedule_id']}_{rec_idx}",
                    config={"toImageButtonOptions": {"format": "png", "filename": heatmap_filename.rsplit(".", 1)[0], "height": heatmap_height, "width": 1800, "scale": 3}, "displayModeBar": True},
                )
                try:
                    heatmap_bytes = fig_heatmap.to_image(format="png", width=1800, height=heatmap_height, scale=3)
                    st.download_button("📥 Download Heatmap PNG", heatmap_bytes, file_name=heatmap_filename, mime="image/png", key=f"saved_heatmap_download_{record['schedule_id']}")
                except Exception:
                    pass

            with st.expander("Revision History"):
                revisions = load_schedule_revisions(saved_date, record["shift_name"], record["power_unit"])
                history_df = pd.DataFrame([
                    {
                        "Revision": f"v{_revision_number(r)}",
                        "Current": "Yes" if safe_bool(r.get("is_current", False)) else "",
                        "Uploaded By": r.get("uploader") or "—",
                        "Uploaded At": datetime.fromisoformat(str(r["uploaded_at"])).strftime("%Y-%m-%d %H:%M:%S"),
                        "Restored From": r.get("restored_from_revision") or "",
                    }
                    for r in revisions
                ])
                st.dataframe(history_df, use_container_width=True, hide_index=True)

                revision_options = {_revision_number(r): r for r in revisions}
                selected_revision_num = st.selectbox(
                    "Revision to preview / restore",
                    options=sorted(revision_options.keys(), reverse=True),
                    format_func=lambda x: f"v{x}" + (" — current" if safe_bool(revision_options[x].get("is_current", False)) else ""),
                    key=f"revision_select_{record['schedule_id']}",
                )
                selected_revision = revision_options[selected_revision_num]
                selected_break_map = load_break_rows_for_schedule_ids([selected_revision["schedule_id"]])
                selected_schedule = schedule_from_break_rows(selected_break_map.get(str(selected_revision["schedule_id"]), []))
                if selected_schedule:
                    st.dataframe(schedule_to_display_df(selected_schedule), use_container_width=True, hide_index=True)

                restore_col1, restore_col2 = st.columns([2, 1])
                restore_by = restore_col1.text_input(
                    "Restored by",
                    placeholder="Name or initials",
                    key=f"restore_by_{record['schedule_id']}",
                ).strip()
                restore_clicked = restore_col2.button(
                    f"♻️ Restore v{selected_revision_num}",
                    disabled=safe_bool(selected_revision.get("is_current", False)) or not bool(selected_schedule),
                    use_container_width=True,
                    key=f"restore_btn_{record['schedule_id']}_{selected_revision_num}",
                )
                if restore_clicked:
                    if not restore_by:
                        st.error("Enter a name or initials in 'Restored by' before restoring a revision.")
                    else:
                        uploaded_at, new_revision, _ = restore_schedule_revision(selected_revision, selected_schedule, restore_by)
                        st.success(f"Restored v{selected_revision_num} as new current revision v{new_revision} at {uploaded_at}.")
                        st.rerun()

    with st.expander("Storage note"):
        st.caption(
            "Schedules are stored in a private Google Sheet. Streamlit only holds the generated schedule in session while it is being reviewed; "
            "published Calendar revisions remain in Google Sheets across Streamlit restarts and redeployments."
        )


def render_storage_setup():
    st.subheader("Google Sheets connection")
    try:
        status = storage_connection_status()
        st.success(f"✅ Connected to private Google Sheet: {status['title']}")
        st.caption("The app will automatically create/use the Schedules and Breaks tabs. Every save is append-only and previous revisions are retained.")
        st.link_button("Open storage spreadsheet", f"https://docs.google.com/spreadsheets/d/{status['spreadsheet_id']}")
    except Exception as exc:
        st.error(f"Not connected yet: {exc}")

    st.markdown("### Free setup")
    st.markdown(
        "1. Create a private Google Sheet.  \n"
        "2. In Google Cloud, enable the Google Sheets API and create a Service Account + JSON key.  \n"
        "3. Share the private Sheet with the service account email as **Editor**.  \n"
        "4. Copy the spreadsheet ID from the Sheet URL.  \n"
        "5. In Streamlit Community Cloud, open **App → Settings → Secrets** and paste the configuration below, replacing the placeholders with values from the JSON key.  \n"
        "6. Never commit the real secrets file or JSON key to GitHub."
    )
    secrets_example = (
        '[break_planner]\n'
        'spreadsheet_id = "YOUR_SPREADSHEET_ID"\n\n'
        '[gcp_service_account]\n'
        'type = "service_account"\n'
        'project_id = "YOUR_PROJECT_ID"\n'
        'private_key_id = "YOUR_PRIVATE_KEY_ID"\n'
        'private_key = "\"\"-----BEGIN PRIVATE KEY-----\nYOUR_PRIVATE_KEY\n-----END PRIVATE KEY-----\n\"\""\n'
        'client_email = "YOUR_SERVICE_ACCOUNT@YOUR_PROJECT.iam.gserviceaccount.com"\n'
        'client_id = "YOUR_CLIENT_ID"\n'
        'auth_uri = "https://accounts.google.com/o/oauth2/auth"\n'
        'token_uri = "https://oauth2.googleapis.com/token"\n'
        'auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"\n'
        'client_x509_cert_url = "YOUR_CLIENT_X509_CERT_URL"'
    )
    st.code(secrets_example, language="toml")

    st.markdown("### Legacy SQLite migration")
    if legacy_db_exists():
        st.info(f"Legacy SQLite database detected at {LEGACY_DB_PATH}. The importer skips Date + Shift + PU combinations already present in Google Sheets.")
        if st.button("Import legacy SQLite schedules into Google Sheets", type="primary"):
            try:
                migrated, skipped = migrate_legacy_sqlite_to_sheets()
                st.success(f"Migration complete: {migrated} imported, {skipped} skipped because they already existed.")
            except Exception as exc:
                st.error(f"Migration failed: {exc}")
    else:
        st.caption("No legacy break_schedules.db file is present in this deployment. Nothing needs to be migrated here.")



if page == "Schedule Calendar":
    render_schedule_calendar()
    st.stop()

if page == "Storage & Setup":
    render_storage_setup()
    st.stop()


# ==========================================
# 5. SCHEDULE DETAILS & MODERATOR DATA TABLE
# ==========================================
st.subheader("Schedule Details")
detail_col1, detail_col2, detail_col3, detail_col4 = st.columns([1.0, 1.0, 1.4, 1.2])
with detail_col1:
    schedule_date = st.date_input(
        "Date",
        value=datetime.now(TURKEY_TZ).date(),
        key="planner_schedule_date",
    )
with detail_col2:
    if shift_preset == "Custom":
        schedule_shift_name = st.text_input(
            "Shift", value="Custom", key="planner_custom_shift_name"
        ).strip()
    else:
        schedule_shift_name = st.text_input(
            "Shift", value=shift_preset, disabled=True, key=f"planner_shift_{shift_preset}"
        ).strip()
with detail_col3:
    power_unit = st.text_input(
        "Power Unit",
        placeholder="e.g. HLC TR",
        key="planner_power_unit",
    ).strip()
with detail_col4:
    uploader_name = st.text_input(
        "Prepared / Uploaded by",
        placeholder="Name or initials",
        key="planner_uploader_name",
        help="Optional signature shown in the saved Schedule Calendar entry.",
    ).strip()

st.caption(
    "Date, Shift and Power Unit are embedded into timetable/heatmap exports and their filenames. "
    "You can either generate and publish immediately, or generate first, review the full schedule, then use "
    "Save Current Generated Schedule to Calendar."
)

st.subheader("Moderator List & Entitlements")
st.caption(
    "Break order is otherwise arbitrary, but Meal can never be the first break. "
    "Meal Exception is automatic: if WB70s > 0, that moderator's Meal is not restricted to the normal Meal Window. "
    "WB70 Duration (mins) is optional: leave it blank to use the universal WB70 duration from the sidebar, "
    "or enter a moderator-specific duration such as 40, 50 or 60 minutes. "
    "Use the WB70 Placement toggle in the sidebar to allow or prevent non-fixed WB70s from entering the second half of the shift; "
    "Fixed WB70 Start always overrides that toggle. "
    "Tick Ticket Moderator for moderators who must maintain ticket coverage; the optimizer will never place all designated ticket moderators on break at the same time."
)

default_data = [
    {"Name": "Alper Uçar", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Arda Su Topcu", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Asiye Sağir", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Baki Doğan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Çağtay Kaplan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Damla Özçelik", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Ege Saritaş", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Ege Solaker", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Gökay Deniz Akçayöz", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Gülsena Kaya", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Hilay Özgü Öztürk", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "İrem Kındıra", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Kadirhan Tekin", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Saim Varol", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
    {"Name": "Zeynep Öykü Ercan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "WB70 Duration (mins)": None, "Fixed WB70 Start": "", "Ticket Moderator": False},
]

edited_df = st.data_editor(
    pd.DataFrame(default_data),
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "Ticket Moderator": st.column_config.CheckboxColumn(
            "Ticket Moderator",
            help="Designated ticket moderators are protected so at least one remains on duty at every 5-minute interval.",
            default=False,
        ),
        "WB70 Duration (mins)": st.column_config.NumberColumn(
            "WB70 Duration (mins)",
            help=(
                "Optional moderator-specific WB70 duration. Leave blank to use the universal WB70 duration "
                "from the sidebar. Examples: 40, 50, 60."
            ),
            min_value=5,
            step=5,
            format="%d",
        ),
        "Fixed WB70 Start": st.column_config.TextColumn(
            "Fixed WB70 Start",
            help="Optional exact WB70 start time in HH:MM format.",
        ),
    },
)

# ==========================================
# 6. SOLVER ENGINE
# ==========================================
button_col1, button_col2 = st.columns(2)
generate_clicked = button_col1.button(
    "🚀 Generate Optimized Schedule",
    type="primary",
    use_container_width=True,
)
generate_save_clicked = button_col2.button(
    "💾 Generate & Save to Calendar",
    use_container_width=True,
)

if generate_clicked or generate_save_clicked:
    with st.spinner("Calculating optimized break layout..."):
        try:
            if not power_unit:
                st.error("❌ Power Unit is required before generating a schedule.")
                st.stop()
            if not schedule_shift_name:
                st.error("❌ Shift name is required before generating a schedule.")
                st.stop()

            base_dt = datetime.combine(schedule_date, datetime.min.time())
            shift_start_dt = parse_time(shift_start_str, base_dt)
            shift_end_dt = parse_time(shift_end_str, base_dt)

            if shift_start_dt is None or shift_end_dt is None:
                st.error("❌ Invalid Shift Start or Shift End. Use HH:MM format.")
                st.stop()

            crosses_midnight = False
            if shift_end_dt <= shift_start_dt:
                shift_end_dt += timedelta(days=1)
                crosses_midnight = True

            def adjust_dt(dt):
                if dt is None:
                    return None
                if crosses_midnight and dt.time() < shift_start_dt.time():
                    return dt + timedelta(days=1)
                return dt

            earliest_dt = adjust_dt(parse_time(earliest_break_str, base_dt))
            final_dt = adjust_dt(parse_time(final_break_str, base_dt))
            meal_win_start = adjust_dt(parse_time(meal_start_str, base_dt))
            meal_win_end = adjust_dt(parse_time(meal_end_str, base_dt))

            if any(x is None for x in [earliest_dt, final_dt, meal_win_start, meal_win_end]):
                st.error("❌ Invalid Earliest/Final/Meal Window time. Use HH:MM format.")
                st.stop()

            if min_gap > max_gap:
                st.error("❌ Minimum Inside Time cannot be greater than Maximum Inside Time.")
                st.stop()

            total_shift_mins = int((shift_end_dt - shift_start_dt).total_seconds() / 60)
            earliest_mins = int((earliest_dt - shift_start_dt).total_seconds() / 60)
            final_mins = int((final_dt - shift_start_dt).total_seconds() / 60)
            meal_start_mins = int((meal_win_start - shift_start_dt).total_seconds() / 60)
            meal_end_mins = int((meal_win_end - shift_start_dt).total_seconds() / 60)

            if earliest_mins > max_gap:
                st.error(
                    f"❌ Logical Conflict: Earliest Break is {earliest_mins} mins into the shift, "
                    f"but Maximum Inside Time is {max_gap} mins."
                )
                st.stop()

            if total_shift_mins - final_mins > max_gap:
                st.error(
                    f"❌ Logical Conflict: Final Break limit leaves {total_shift_mins - final_mins} mins "
                    f"before shift end, above the Maximum Inside Time of {max_gap} mins."
                )
                st.stop()

            if final_mins <= earliest_mins:
                st.error("❌ Final Break limit must occur after Earliest Break.")
                st.stop()

            # Build moderator records and cache candidate sets by identical rule profile.
            moderators = []
            profile_cache = {}

            for row_idx, row in edited_df.iterrows():
                name = str(row.get("Name", "")).strip()
                if not name or name.lower() == "nan":
                    continue

                counts = {
                    "Short": safe_nonnegative_int(row.get("Shorts", 0)),
                    "Meal": safe_nonnegative_int(row.get("Meals", 0)),
                    "WB20": safe_nonnegative_int(row.get("WB20s", 0)),
                    "WB70": safe_nonnegative_int(row.get("WB70s", 0)),
                }
                if sum(counts.values()) == 0:
                    continue

                # Optional per-moderator WB70 duration override.
                # Blank = use the universal WB70 duration configured in the sidebar.
                wb70_duration_raw = row.get("WB70 Duration (mins)", None)
                wb70_duration_override = None
                if wb70_duration_raw is not None and not pd.isna(wb70_duration_raw) and str(wb70_duration_raw).strip() != "":
                    try:
                        numeric_duration = float(wb70_duration_raw)
                        if not numeric_duration.is_integer() or numeric_duration <= 0:
                            raise ValueError
                        wb70_duration_override = int(numeric_duration)
                    except Exception:
                        st.error(
                            f"❌ {name} has an invalid WB70 Duration. Enter a positive whole number of minutes "
                            "(for example 40, 50 or 60), or leave the field blank."
                        )
                        st.stop()

                if wb70_duration_override is not None and counts["WB70"] == 0:
                    st.error(
                        f"❌ {name} has a WB70 Duration override but WB70s is 0. "
                        "Either clear the duration override or give the moderator a WB70 entitlement."
                    )
                    st.stop()

                effective_durations = dict(DURATIONS)
                if counts["WB70"] > 0 and wb70_duration_override is not None:
                    effective_durations["WB70"] = wb70_duration_override

                fixed_dt = adjust_dt(parse_time(row.get("Fixed WB70 Start", ""), base_dt))
                fixed_mins = None
                if fixed_dt is not None:
                    fixed_mins = int((fixed_dt - shift_start_dt).total_seconds() / 60)

                if fixed_mins is not None and counts["WB70"] == 0:
                    st.error(
                        f"❌ {name} has a Fixed WB70 Start but WB70s is 0. "
                        "Either clear the fixed time or give the moderator a WB70 entitlement."
                    )
                    st.stop()

                ticket_moderator = safe_bool(row.get("Ticket Moderator", False))

                profile = (
                    tuple((b, counts[b]) for b in BREAK_TYPES),
                    tuple((b, effective_durations[b]) for b in BREAK_TYPES),
                    fixed_mins,
                    bool(allow_wb70_second_half),
                )

                if profile not in profile_cache:
                    patterns = build_candidate_patterns(
                        counts=counts,
                        durations=effective_durations,
                        total_shift_mins=total_shift_mins,
                        earliest_mins=earliest_mins,
                        final_mins=final_mins,
                        meal_start_mins=meal_start_mins,
                        meal_end_mins=meal_end_mins,
                        min_inside=min_gap,
                        max_inside=max_gap,
                        fixed_wb70_mins=fixed_mins,
                        allow_wb70_second_half=allow_wb70_second_half,
                    )
                    profile_cache[profile] = patterns

                patterns = profile_cache[profile]
                if not patterns:
                    extra = ""
                    if fixed_mins is not None:
                        extra += f" Fixed WB70 start: {row.get('Fixed WB70 Start', '')}."
                    if counts["WB70"] > 0:
                        extra += f" WB70 duration: {effective_durations['WB70']} minutes."
                        if not allow_wb70_second_half and fixed_mins is None:
                            midpoint_dt = shift_start_dt + timedelta(minutes=total_shift_mins / 2.0)
                            extra += f" Non-fixed WB70 must finish by shift midpoint ({midpoint_dt.strftime('%H:%M')})."
                    st.error(
                        f"❌ No individually feasible break layout exists for {name} under the current rules.{extra} "
                        "This is a genuine moderator-level rule conflict, not a solver timeout."
                    )
                    st.stop()

                moderators.append(
                    {
                        "Name": name,
                        "Counts": counts,
                        "Durations": effective_durations,
                        "WB70DurationOverride": wb70_duration_override,
                        "FixedWB70": fixed_mins,
                        "TicketModerator": ticket_moderator,
                        "Profile": profile,
                    }
                )

            if not moderators:
                st.error("❌ No moderators with break entitlements were provided.")
                st.stop()

            ticket_moderators = [m for m in moderators if m.get("TicketModerator", False)]
            if len(ticket_moderators) == 1:
                st.error(
                    f"❌ Only one Ticket Moderator is selected ({ticket_moderators[0]['Name']}). "
                    "The rule requires at least one ticket moderator to remain on duty, so a single designated moderator could never take a break. "
                    "Select at least two Ticket Moderators or clear the checkbox."
                )
                st.stop()

            timeline_mins = list(range(0, total_shift_mins + 1, TIME_STEP))
            pressure_profile = build_pressure_profile(
                shift_preset, shift_start_dt, timeline_mins
            )
            pressure_weights = pressure_profile["Weight"]

            # Each moderator references the cached candidate pool for their profile.
            pattern_sets = []
            vector_sets = []
            vector_cache = {}

            for mod in moderators:
                patterns = profile_cache[mod["Profile"]]
                pattern_sets.append(patterns)

                if mod["Profile"] not in vector_cache:
                    active_cols = []
                    wb_cols = []
                    for pattern in patterns:
                        a, w = pattern_vectors(pattern, mod["Durations"], timeline_mins)
                        active_cols.append(a)
                        wb_cols.append(w)
                    vector_cache[mod["Profile"]] = (
                        np.stack(active_cols, axis=1),
                        np.stack(wb_cols, axis=1),
                    )

                vector_sets.append(vector_cache[mod["Profile"]])

            result = optimize_pattern_selection(
                moderators, pattern_sets, vector_sets, timeline_mins, pressure_weights
            )

            schedule = []
            for m_idx, mod in enumerate(moderators):
                pattern = pattern_sets[m_idx][result["Chosen"][m_idx]]
                for b_type, start_min in zip(pattern["Order"], pattern["Starts"]):
                    start_dt = shift_start_dt + timedelta(minutes=int(start_min))
                    duration_mins = mod["Durations"][b_type]
                    end_dt = start_dt + timedelta(minutes=duration_mins)
                    start_str = start_dt.strftime("%H:%M")
                    end_str = end_dt.strftime("%H:%M")

                    if duration_mins <= 20:
                        bar_text = f"<b>{start_str}<br>{end_str}</b>"
                    else:
                        bar_text = f"<b>{start_str}-{end_str}</b>"

                    schedule.append(
                        {
                            "Name": mod["Name"],
                            "Task": f"<b>{mod['Name']}</b>",
                            "Resource": b_type,
                            "Start": start_dt,
                            "Finish": end_dt,
                            "Bar_Text": bar_text,
                            "TicketModerator": bool(mod.get("TicketModerator", False)),
                        }
                    )

            if not schedule:
                st.error("❌ No schedule could be constructed.")
                st.stop()

            ticket_count = len(ticket_moderators)
            min_ticket_on_duty = None
            if ticket_count >= 2:
                ticket_names = {m["Name"] for m in ticket_moderators}
                ticket_break_counts = []
                for t in range(0, total_shift_mins + 1, TIME_STEP):
                    t_dt = shift_start_dt + timedelta(minutes=t)
                    count_on_break = sum(
                        1 for b in schedule
                        if b.get("Name") in ticket_names and b["Start"] <= t_dt < b["Finish"]
                    )
                    ticket_break_counts.append(count_on_break)
                min_ticket_on_duty = ticket_count - max(ticket_break_counts, default=0)
                if min_ticket_on_duty < 1:
                    st.error("❌ Internal validation failed: all Ticket Moderators overlap on break at least once.")
                    st.stop()

            sched_df = pd.DataFrame(schedule).sort_values(
                by=["Task", "Start"], ascending=[False, True]
            )

            timeline_dts = [shift_start_dt + timedelta(minutes=t) for t in timeline_mins]
            concurrency_counts = [
                sum(1 for b in schedule if b["Start"] <= t_dt < b["Finish"])
                for t_dt in timeline_dts
            ]
            concurrency_df = pd.DataFrame(
                {
                    "Time": timeline_dts,
                    "Concurrent Breaks": concurrency_counts,
                    "Pressure Weight": pressure_weights,
                    "Raw Volume": pressure_profile["RawVolume"],
                    "Effective Pressure": pressure_profile["EffectivePressure"],
                    "Pressure Source": pressure_profile["Label"],
                }
            )

            current_payload = {
                "schedule_date": schedule_date,
                "schedule_shift_name": schedule_shift_name,
                "power_unit": power_unit,
                "uploader": uploader_name,
                "shift_start_str": shift_start_str,
                "shift_end_str": shift_end_str,
                "earliest_dt": earliest_dt,
                "final_dt": final_dt,
                "schedule": schedule,
                "peak_concurrent": int(result["Peak"]),
                "peak_wb70": int(result["WB70Peak"]),
                "ticket_moderator_count": ticket_count,
                "min_ticket_on_duty": min_ticket_on_duty,
                "allow_wb70_second_half": bool(allow_wb70_second_half),
                "wb70_shift_midpoint": shift_start_dt + timedelta(minutes=total_shift_mins / 2.0),
                "used_fallback": bool(result["UsedFallback"]),
                "concurrency_df": concurrency_df,
                "shift_preset": shift_preset,
                "generated_at": datetime.now(TURKEY_TZ).isoformat(timespec="seconds"),
            }
            st.session_state["current_generated_schedule"] = current_payload

            if generate_save_clicked:
                uploaded_at, new_revision, had_previous = save_schedule_record(
                    schedule_date=schedule_date,
                    shift_name=schedule_shift_name,
                    power_unit=power_unit,
                    uploader=uploader_name,
                    shift_start=shift_start_str,
                    shift_end=shift_end_str,
                    earliest_dt=earliest_dt,
                    final_dt=final_dt,
                    schedule=schedule,
                    peak_concurrent=result["Peak"],
                    peak_wb70=result["WB70Peak"],
                )
                uploaded_display = datetime.fromisoformat(uploaded_at).strftime("%Y-%m-%d %H:%M:%S")
                st.session_state["current_schedule_save_notice"] = (
                    f"💾 Schedule saved to Calendar as revision v{new_revision} for {schedule_date.strftime('%Y-%m-%d')} "
                    f"• {schedule_shift_name} • {power_unit}. Upload time: {uploaded_display} (Türkiye time)."
                )


        except Exception as exc:
            st.error(f"An unexpected error occurred during scheduling calculation: {str(exc)}")


# ==========================================
# 7. CURRENT GENERATED SCHEDULE: REVIEW, THEN SAVE
# ==========================================
current_payload = st.session_state.get("current_generated_schedule")
if current_payload:
    st.markdown("---")
    st.subheader("Current Generated Schedule")
    generated_at = datetime.fromisoformat(current_payload["generated_at"])
    st.caption(
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M:%S')} (Türkiye time) • "
        f"Date: {current_payload['schedule_date'].strftime('%Y-%m-%d')} • "
        f"Shift: {current_payload['schedule_shift_name']} • Power Unit: {current_payload['power_unit']}. "
        "This is a saved-in-session snapshot of the last generated result, so you can review it before publishing it to the Calendar."
    )

    notice = st.session_state.pop("current_schedule_save_notice", None)
    if notice:
        st.success(notice)

    save_col, note_col = st.columns([1.3, 3.7])
    with save_col:
        save_current_clicked = st.button(
            "💾 Save Current Generated Schedule to Calendar",
            type="primary",
            use_container_width=True,
            key="save_current_generated_schedule",
        )
    with note_col:
        st.caption(
            "Date, Shift and Power Unit are taken from the generated schedule above. "
            "The current Prepared / Uploaded by field is used as the signature. Saving creates a new immutable revision; it does not erase the previous Calendar version."
        )

    if save_current_clicked:
        try:
            uploaded_at, new_revision, had_previous = save_schedule_record(
                schedule_date=current_payload["schedule_date"],
                shift_name=current_payload["schedule_shift_name"],
                power_unit=current_payload["power_unit"],
                uploader=uploader_name,
                shift_start=current_payload["shift_start_str"],
                shift_end=current_payload["shift_end_str"],
                earliest_dt=current_payload["earliest_dt"],
                final_dt=current_payload["final_dt"],
                schedule=current_payload["schedule"],
                peak_concurrent=current_payload["peak_concurrent"],
                peak_wb70=current_payload["peak_wb70"],
            )
            # Keep the session snapshot's displayed signature aligned with the save.
            current_payload["uploader"] = uploader_name
            st.session_state["current_generated_schedule"] = current_payload
            uploaded_display = datetime.fromisoformat(uploaded_at).strftime("%Y-%m-%d %H:%M:%S")
            st.success(
                f"💾 Current generated schedule saved to Calendar as revision v{new_revision} for "
                f"{current_payload['schedule_date'].strftime('%Y-%m-%d')} • "
                f"{current_payload['schedule_shift_name']} • {current_payload['power_unit']}. "
                f"Upload time: {uploaded_display} (Türkiye time)."
            )
        except Exception as exc:
            st.error(f"Could not save the current generated schedule: {str(exc)}")

    render_generated_schedule(current_payload)
