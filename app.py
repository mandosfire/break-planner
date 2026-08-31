import streamlit as st
import pandas as pd
import plotly.express as px
import numpy as np
import scipy.sparse as sp
from scipy.optimize import milp, LinearConstraint, Bounds
from datetime import datetime, timedelta
from itertools import permutations
import random
import math
import hashlib

# ==========================================
# 1. PAGE CONFIGURATION & UI SETUP
# ==========================================
st.set_page_config(page_title="Lark Break Planner", layout="wide")
st.title("Shift Break Optimizer")
st.markdown(
    "Maximize on-duty staff while strictly enforcing meal windows, shift limits, "
    "inside-time rules, fixed WB70 times, and moderator break entitlements."
)

# ==========================================
# 2. SIDEBAR RULES & CONFIGURATION
# ==========================================
st.sidebar.header("Global Shift Rules")

shift_start_str = st.sidebar.text_input("Shift Start", value="07:30")
shift_end_str = st.sidebar.text_input("Shift End", value="16:30")
earliest_break_str = st.sidebar.text_input("Earliest Break Allowed", value="08:30")
final_break_str = st.sidebar.text_input("Final Break Must End By", value="15:45")

st.sidebar.markdown("---")
meal_start_str = st.sidebar.text_input("Meal Window Start", value="11:30")
meal_end_str = st.sidebar.text_input("Meal Window End", value="14:30")

st.sidebar.markdown("---")
min_gap = int(st.sidebar.number_input("Minimum Inside Time (mins)", value=45, step=5))
max_gap = int(st.sidebar.number_input("Maximum Inside Time (mins)", value=105, step=5))

st.sidebar.markdown("---")
st.sidebar.subheader("Break Durations (mins)")
dur_short = int(st.sidebar.number_input("Short Break", value=15, step=5))
dur_meal = int(st.sidebar.number_input("Meal Break", value=30, step=5))
dur_wb20 = int(st.sidebar.number_input("WB20 Break", value=20, step=5))
dur_wb70 = int(st.sidebar.number_input("WB70 Break", value=70, step=5))

DURATIONS = {
    "Short": dur_short,
    "Meal": dur_meal,
    "WB20": dur_wb20,
    "WB70": dur_wb70,
}
BREAK_TYPES = ["Short", "Meal", "WB20", "WB70"]
TIME_STEP = 5
MAX_PATTERNS_PER_PROFILE = 300
SOLVER_TIME_LIMIT = 60
MIP_REL_GAP = 0.05

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


def ceil_step(value, step=TIME_STEP):
    return int(math.ceil(value / step) * step)


def floor_step(value, step=TIME_STEP):
    return int(math.floor(value / step) * step)


def unique_break_orders(counts):
    """Return arbitrary unique type orders for the entitlement multiset."""
    items = []
    for b_type in BREAK_TYPES:
        items.extend([b_type] * counts.get(b_type, 0))

    # Normal planner use is 4-6 breaks. For unusually large counts, sample orders
    # instead of materializing an enormous permutation set.
    if len(items) <= 8:
        return list(set(permutations(items)))

    rng = random.Random(81731)
    seen = set()
    base = list(items)
    for _ in range(800):
        rng.shuffle(base)
        seen.add(tuple(base))
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
    max_patterns=MAX_PATTERNS_PER_PROFILE,
):
    """
    Generate complete individually-feasible schedules first.

    Each candidate already satisfies:
      - exact entitlements
      - arbitrary break-type order
      - earliest/final break limits
      - min/max inside time before, between and after breaks
      - normal Meal Window unless WB70 exists
      - exact Fixed WB70 start when provided
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
                    if (
                        fixed_wb70_mins < low
                        or fixed_wb70_mins > high
                        or fixed_wb70_mins % TIME_STEP != 0
                    ):
                        candidate_starts = []
                    else:
                        candidate_starts = [fixed_wb70_mins]
                else:
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


def greedy_fallback(moderators, pattern_sets, vector_sets, timeline_len):
    """Always produce a complete feasible selection if individual candidates exist."""
    overall = np.zeros(timeline_len, dtype=float)
    wb70 = np.zeros(timeline_len, dtype=float)
    chosen = {}

    # Place WB70 moderators first because they are operationally more restrictive.
    order = sorted(
        range(len(moderators)),
        key=lambda i: moderators[i]["Counts"].get("WB70", 0),
        reverse=True,
    )

    for m_idx in order:
        best_idx = None
        best_score = None
        active_mat, wb_mat = vector_sets[m_idx]

        for p_idx in range(active_mat.shape[1]):
            new_overall = overall + active_mat[:, p_idx]
            new_wb = wb70 + wb_mat[:, p_idx]
            # Same priority structure as the MILP, with a quadratic smoothing term.
            score = (
                3000 * np.max(new_wb)
                + 1000 * np.max(new_overall)
                + np.sum(new_overall * (new_overall + 1) / 2)
            )
            if best_score is None or score < best_score:
                best_score = score
                best_idx = p_idx

        chosen[m_idx] = best_idx
        overall += active_mat[:, best_idx]
        wb70 += wb_mat[:, best_idx]

    return chosen, int(np.max(overall)), int(np.max(wb70))


def optimize_pattern_selection(moderators, pattern_sets, vector_sets, timeline_mins):
    """
    Set-partitioning MILP: choose exactly one complete feasible pattern per moderator.

    This model is dramatically smaller than the previous break-level formulation,
    so time limits no longer get confused with individual schedule infeasibility.
    """
    moderator_count = len(moderators)
    timeline_len = len(timeline_mins)

    # One binary variable for every moderator/candidate-pattern pair.
    y_offsets = []
    cursor = 0
    for patterns in pattern_sets:
        y_offsets.append(cursor)
        cursor += len(patterns)
    n_y = cursor

    idx_max_concurrent = n_y
    idx_max_wb70 = n_y + 1
    idx_e = n_y + 2
    n_e = timeline_len * moderator_count
    n_vars = n_y + 2 + n_e

    c = np.zeros(n_vars, dtype=float)
    c[idx_max_concurrent] = 1000.0
    c[idx_max_wb70] = 3000.0

    # Triangular occupancy penalty: equivalent to 1+2+...+load at each time.
    for t_idx in range(timeline_len):
        for k in range(moderator_count):
            c[idx_e + t_idx * moderator_count + k] = float(k + 1)

    integrality = np.zeros(n_vars, dtype=int)
    integrality[:n_y] = 1
    integrality[idx_max_concurrent] = 1
    integrality[idx_max_wb70] = 1

    lower = np.zeros(n_vars, dtype=float)
    upper = np.full(n_vars, np.inf, dtype=float)
    upper[:n_y] = 1.0
    upper[idx_max_concurrent] = float(moderator_count)
    upper[idx_max_wb70] = float(moderator_count)
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

        for m_idx in range(moderator_count):
            active_mat, wb_mat = vector_sets[m_idx]
            off = y_offsets[m_idx]
            for p_idx in range(active_mat.shape[1]):
                a = active_mat[t_idx, p_idx]
                w = wb_mat[t_idx, p_idx]
                if a:
                    load_terms[off + p_idx] = float(a)
                if w:
                    wb_terms[off + p_idx] = float(w)

        row = dict(load_terms)
        row[idx_max_concurrent] = -1.0
        rows.append(row)
        lbs.append(-np.inf)
        ubs.append(0.0)

        row = dict(wb_terms)
        row[idx_max_wb70] = -1.0
        rows.append(row)
        lbs.append(-np.inf)
        ubs.append(0.0)

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
            moderators, pattern_sets, vector_sets, timeline_len
        )
        return {
            "Chosen": chosen,
            "Peak": peak,
            "WB70Peak": wb_peak,
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
        "UsedFallback": not bool(result.success),
        "SolverMessage": result.message,
    }


# ==========================================
# 4. MODERATOR DATA TABLE
# ==========================================
st.subheader("Moderator List & Entitlements")
st.caption(
    "Break order is fully arbitrary. Meal Exception is automatic: if WB70s > 0, "
    "that moderator's Meal is not restricted to the normal Meal Window."
)

default_data = [
    {"Name": "Alper Uçar", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Arda Su Topcu", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Asiye Sağir", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Baki Doğan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Çağtay Kaplan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Damla Özçelik", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Ege Saritaş", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Ege Solaker", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "Fixed WB70 Start": ""},
    {"Name": "Gökay Deniz Akçayöz", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Gülsena Kaya", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Hilay Özgü Öztürk", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "İrem Kındıra", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "Fixed WB70 Start": ""},
    {"Name": "Kadirhan Tekin", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
    {"Name": "Saim Varol", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "Fixed WB70 Start": ""},
    {"Name": "Zeynep Öykü Ercan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Fixed WB70 Start": ""},
]

edited_df = st.data_editor(pd.DataFrame(default_data), num_rows="dynamic", use_container_width=True)

# ==========================================
# 5. SOLVER ENGINE
# ==========================================
if st.button("🚀 Generate Optimized Schedule", type="primary"):
    with st.spinner("Calculating optimized break layout..."):
        try:
            base_dt = datetime(2026, 1, 1)
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

                profile = (
                    tuple((b, counts[b]) for b in BREAK_TYPES),
                    fixed_mins,
                )

                if profile not in profile_cache:
                    patterns = build_candidate_patterns(
                        counts=counts,
                        durations=DURATIONS,
                        total_shift_mins=total_shift_mins,
                        earliest_mins=earliest_mins,
                        final_mins=final_mins,
                        meal_start_mins=meal_start_mins,
                        meal_end_mins=meal_end_mins,
                        min_inside=min_gap,
                        max_inside=max_gap,
                        fixed_wb70_mins=fixed_mins,
                    )
                    profile_cache[profile] = patterns

                patterns = profile_cache[profile]
                if not patterns:
                    extra = ""
                    if fixed_mins is not None:
                        extra = f" Fixed WB70 start: {row.get('Fixed WB70 Start', '')}."
                    st.error(
                        f"❌ No individually feasible break layout exists for {name} under the current rules.{extra} "
                        "This is a genuine moderator-level rule conflict, not a solver timeout."
                    )
                    st.stop()

                moderators.append(
                    {
                        "Name": name,
                        "Counts": counts,
                        "FixedWB70": fixed_mins,
                        "Profile": profile,
                    }
                )

            if not moderators:
                st.error("❌ No moderators with break entitlements were provided.")
                st.stop()

            timeline_mins = list(range(0, total_shift_mins + 1, TIME_STEP))

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
                        a, w = pattern_vectors(pattern, DURATIONS, timeline_mins)
                        active_cols.append(a)
                        wb_cols.append(w)
                    vector_cache[mod["Profile"]] = (
                        np.stack(active_cols, axis=1),
                        np.stack(wb_cols, axis=1),
                    )

                vector_sets.append(vector_cache[mod["Profile"]])

            result = optimize_pattern_selection(
                moderators, pattern_sets, vector_sets, timeline_mins
            )

            schedule = []
            for m_idx, mod in enumerate(moderators):
                pattern = pattern_sets[m_idx][result["Chosen"][m_idx]]
                for b_type, start_min in zip(pattern["Order"], pattern["Starts"]):
                    start_dt = shift_start_dt + timedelta(minutes=int(start_min))
                    end_dt = start_dt + timedelta(minutes=DURATIONS[b_type])
                    duration_mins = DURATIONS[b_type]
                    start_str = start_dt.strftime("%H:%M")
                    end_str = end_dt.strftime("%H:%M")

                    if duration_mins <= 20:
                        bar_text = f"<b>{start_str}<br>{end_str}</b>"
                    else:
                        bar_text = f"<b>{start_str}-{end_str}</b>"

                    schedule.append(
                        {
                            "Task": f"<b>{mod['Name']}</b>",
                            "Resource": b_type,
                            "Start": start_dt,
                            "Finish": end_dt,
                            "Bar_Text": bar_text,
                        }
                    )

            if not schedule:
                st.error("❌ No schedule could be constructed.")
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
                {"Time": timeline_dts, "Concurrent Breaks": concurrency_counts}
            )

            if result["UsedFallback"]:
                st.warning(
                    "⚠️ The mathematical optimizer reached its time/optimality limit, so the app used its "
                    "complete feasible fallback selection rather than incorrectly reporting the schedule as impossible."
                )

            st.success(
                f"✅ Schedule Generated! Peak concurrent breaks: **{result['Peak']}**  |  "
                f"Peak concurrent WB70s: **{result['WB70Peak']}**"
            )

            # ==========================================
            # 6. DASHBOARD & VISUALIZATION
            # ==========================================
            st.markdown(
                f"<div style='background-color: #1c2838; color: white; padding: 12px; border-radius: 4px; "
                f"text-align: center; font-size: 22px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
                f"Shift Break Timetable &bull; {shift_start_str}-{shift_end_str}</div>",
                unsafe_allow_html=True,
            )
            st.markdown("<br>", unsafe_allow_html=True)

            color_map = {
                "Short": "#3b82f6",
                "Meal": "#f97316",
                "WB20": "#22c55e",
                "WB70": "#a855f7",
            }

            fig_gantt = px.timeline(
                sched_df,
                x_start="Start",
                x_end="Finish",
                y="Task",
                color="Resource",
                text="Bar_Text",
                color_discrete_map=color_map,
            )

            fig_gantt.update_traces(
                textposition="inside",
                insidetextanchor="middle",
                textangle=0,
                textfont=dict(family="Montserrat, sans-serif", color="white", size=11),
                marker=dict(line=dict(width=1, color="rgba(255, 255, 255, 0.6)")),
            )

            num_moderators = len(sched_df["Task"].unique())
            dynamic_height = max(600, num_moderators * 45 + 150)

            fig_gantt.update_layout(
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
                    y=1.02,
                    xanchor="center",
                    x=0.5,
                    title="",
                ),
                margin=dict(l=0, r=0, t=60, b=40),
                height=dynamic_height,
            )

            plotly_config = {
                "toImageButtonOptions": {
                    "format": "png",
                    "filename": f"Break_Timetable_{shift_start_str}_{shift_end_str}",
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
                    file_name=f"Timetable_{shift_start_str}_{shift_end_str}.png",
                    mime="image/png",
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

        except Exception as exc:
            st.error(f"An unexpected error occurred during scheduling calculation: {str(exc)}")
