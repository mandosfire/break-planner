"""Candidate-pattern MILP engine, carried forward from V10.1.
Exact entitlements: 3 Short, 1 Meal, one WB20/WB70. Meal can never be first.
"""
from datetime import datetime, timedelta
from itertools import permutations
import hashlib
import math
import random
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds
import scipy.sparse as sp

TIME_STEP=5
BREAK_TYPES=["Short","Meal","WB20","WB70"]
MAX_PATTERNS_PER_PROFILE=300
SOLVER_TIME_LIMIT=60
MIP_REL_GAP=0.05
BASE_VOLUME=1836.0
OVERLAP_COVERAGE_FACTOR=0.50

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


def greedy_fallback(moderators, pattern_sets, vector_sets, timeline_len, pressure_weights, ticket_available_counts=None):
    """Produce a complete feasible fallback while honoring the ticket-coverage hard rule."""
    ticket_indices = [i for i, mod in enumerate(moderators) if mod.get("TicketModerator", False)]
    ticket_count = len(ticket_indices)
    if ticket_available_counts is None:
        ticket_available_counts = [ticket_count] * timeline_len
    # During mixed shifts, protection is determined by ticket moderators who
    # are actually scheduled at each point, not their total headcount.
    ticket_limits=np.array([max(0,int(n)-1) for n in ticket_available_counts], dtype=float)
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
                    if np.any(candidate_ticket > ticket_limits + 1e-9):
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

        if ticket_count >= 2 and np.any(ticket_active > ticket_limits + 1e-9):
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

def optimize_pattern_selection(moderators, pattern_sets, vector_sets, timeline_mins, pressure_weights, ticket_available_counts=None):
    """
    Set-partitioning MILP: choose exactly one complete feasible pattern per moderator.

    This model is dramatically smaller than the previous break-level formulation,
    so time limits no longer get confused with individual schedule infeasibility.
    """
    moderator_count = len(moderators)
    timeline_len = len(timeline_mins)
    ticket_count = sum(1 for mod in moderators if mod.get("TicketModerator", False))
    if ticket_available_counts is None:
        ticket_available_counts = [ticket_count] * timeline_len
    if len(ticket_available_counts) != timeline_len:
        raise ValueError("Ticket availability must match the optimization timeline.")

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
            ubs.append(float(max(0, ticket_available_counts[t_idx] - 1)))

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
            moderators, pattern_sets, vector_sets, timeline_len, pressure_weights,
            ticket_available_counts=ticket_available_counts,
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

