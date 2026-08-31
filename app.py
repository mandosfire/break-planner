import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pulp
from datetime import datetime, timedelta

# ==========================================
# 1. PAGE CONFIGURATION & UI SETUP
# ==========================================
st.set_page_config(page_title="Lark Break Planner", layout="wide")
st.title("Shift Break Optimizer")
st.markdown("Maximize on-duty staff while strictly enforcing meal windows, shift limits, and gap times.")

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
min_gap = st.sidebar.number_input("Minimum Inside Time (mins)", value=45)
max_gap = st.sidebar.number_input("Maximum Inside Time (mins)", value=105)

st.sidebar.markdown("---")
st.sidebar.subheader("Break Durations (mins)")
dur_short = st.sidebar.number_input("Short Break", value=15)
dur_meal = st.sidebar.number_input("Meal Break", value=30)
dur_wb20 = st.sidebar.number_input("WB20 Break", value=20)
dur_wb70 = st.sidebar.number_input("WB70 Break", value=70)

DURATIONS = {'Short': dur_short, 'Meal': dur_meal, 'WB20': dur_wb20, 'WB70': dur_wb70}
BREAK_TYPES = ['Short', 'Meal', 'WB20', 'WB70']

# ==========================================
# 3. HELPER FUNCTIONS (TIME & VALIDATION)
# ==========================================
def parse_time(time_str, base_date=datetime(2026, 1, 1)):
    """Convert string to datetime without automatic overnight assumptions."""
    if not time_str or pd.isna(time_str) or str(time_str).strip() == "":
        return None
    try:
        h, m = map(int, str(time_str).strip().split(':'))
        return base_date.replace(hour=h, minute=m, second=0)
    except Exception:
        return None


def safe_nonnegative_int(value):
    """Convert editable-table values to a non-negative integer."""
    if pd.isna(value) or value == "":
        return 0
    try:
        return max(0, int(value))
    except Exception:
        return 0


# ==========================================
# 4. MODERATOR DATA TABLE
# ==========================================
st.subheader("Moderator List & Entitlements")
st.caption("Meal Exception is automatic: any moderator with at least one WB70 may have their Meal outside the normal Meal Window.")

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

df = pd.DataFrame(default_data)
edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True)

# ==========================================
# 5. SOLVER ENGINE (PuLP)
# ==========================================
if st.button("🚀 Generate Optimized Schedule", type="primary"):
    with st.spinner("Calculating optimal break layout..."):
        try:
            base_dt = datetime(2026, 1, 1)
            shift_start_dt = parse_time(shift_start_str, base_dt)
            shift_end_dt = parse_time(shift_end_str, base_dt)

            if shift_start_dt is None or shift_end_dt is None:
                st.error("❌ Invalid Shift Start or Shift End time. Use HH:MM format.")
                st.stop()

            # Dynamically detect if shift crosses midnight
            crosses_midnight = False
            if shift_end_dt <= shift_start_dt:
                shift_end_dt += timedelta(days=1)
                crosses_midnight = True

            def adjust_dt(dt):
                """Push times to the next day when they belong to the after-midnight part of an overnight shift."""
                if dt is None:
                    return None
                if crosses_midnight and dt.time() < shift_start_dt.time():
                    return dt + timedelta(days=1)
                return dt

            earliest_dt = adjust_dt(parse_time(earliest_break_str, base_dt))
            final_dt = adjust_dt(parse_time(final_break_str, base_dt))
            meal_win_start = adjust_dt(parse_time(meal_start_str, base_dt))
            meal_win_end = adjust_dt(parse_time(meal_end_str, base_dt))

            if earliest_dt is None or final_dt is None:
                st.error("❌ Invalid Earliest Break or Final Break time. Use HH:MM format.")
                st.stop()

            if meal_win_start is None or meal_win_end is None:
                st.error("❌ Invalid Meal Window time. Use HH:MM format.")
                st.stop()

            if min_gap > max_gap:
                st.error("❌ Logical Conflict: Minimum Inside Time cannot be greater than Maximum Inside Time.")
                st.stop()

            # Proactive Error Checks
            earliest_mins = int((earliest_dt - shift_start_dt).total_seconds() / 60)
            if earliest_mins > max_gap:
                st.error(
                    f"❌ Logical Conflict: 'Earliest Break' is {earliest_mins} mins into the shift, "
                    f"but 'Max Inside Time' is only {max_gap} mins. Increase Max Gap or lower Earliest Break."
                )
                st.stop()

            final_mins_from_end = int((shift_end_dt - final_dt).total_seconds() / 60)
            if final_mins_from_end > max_gap:
                st.error(
                    f"❌ Logical Conflict: 'Final Break' is {final_mins_from_end} mins before shift end, "
                    f"but 'Max Inside Time' is only {max_gap} mins. Increase Max Gap or raise Final Break."
                )
                st.stop()

            total_shift_mins = int((shift_end_dt - shift_start_dt).total_seconds() / 60)
            time_intervals = list(range(0, total_shift_mins + 1, 5))

            prob = pulp.LpProblem("Maximize_On_Duty", pulp.LpMinimize)

            # x[(mod_id, position, break_type, t)] = 1 means:
            # chronological break position `position` is of `break_type` and starts t minutes after shift start.
            # This allows the SOLVER to choose the break-type order instead of using a hard-coded sequence.
            x = {}
            moderator_data = {}
            position_start_expr = {}
            position_duration_expr = {}

            max_concurrent = pulp.LpVariable("Max_Concurrent", lowBound=0, cat='Integer')
            max_wb70_concurrent = pulp.LpVariable("Max_WB70_Concurrent", lowBound=0, cat='Integer')

            # Prepare moderators and create decision variables
            for row_idx, row in edited_df.iterrows():
                name = str(row.get('Name', '')).strip()
                if not name or name.lower() == 'nan':
                    continue

                counts = {
                    'Short': safe_nonnegative_int(row.get('Shorts', 0)),
                    'Meal': safe_nonnegative_int(row.get('Meals', 0)),
                    'WB20': safe_nonnegative_int(row.get('WB20s', 0)),
                    'WB70': safe_nonnegative_int(row.get('WB70s', 0)),
                }
                total_breaks = sum(counts.values())
                if total_breaks == 0:
                    continue

                mod_id = f"m{row_idx}"
                fixed_wb70_dt = adjust_dt(parse_time(row.get('Fixed WB70 Start', ''), base_dt))
                automatic_meal_exception = counts['WB70'] > 0
                allowed_types = [b for b in BREAK_TYPES if counts[b] > 0]

                moderator_data[mod_id] = {
                    'Name': name,
                    'Counts': counts,
                    'Positions': list(range(total_breaks)),
                    'AllowedTypes': allowed_types,
                    'FixedWB70': fixed_wb70_dt,
                    'MealException': automatic_meal_exception,
                }

                # Candidate variables. Illegal type/time combinations are not created at all.
                for pos in range(total_breaks):
                    for b_type in allowed_types:
                        dur = DURATIONS[b_type]
                        for t in time_intervals:
                            actual_time_dt = shift_start_dt + timedelta(minutes=t)
                            finish_time_dt = actual_time_dt + timedelta(minutes=dur)

                            # Global break bounds
                            if actual_time_dt < earliest_dt:
                                continue
                            if finish_time_dt > final_dt or finish_time_dt > shift_end_dt:
                                continue

                            # Meal window, unless moderator has WB70 (automatic Meal Exception)
                            if b_type == 'Meal' and not automatic_meal_exception:
                                if actual_time_dt < meal_win_start or finish_time_dt > meal_win_end:
                                    continue

                            # Optional fixed WB70 start
                            if b_type == 'WB70' and fixed_wb70_dt is not None:
                                if actual_time_dt != fixed_wb70_dt:
                                    continue

                            x[(mod_id, pos, b_type, t)] = pulp.LpVariable(
                                f"x_{mod_id}_{pos}_{b_type}_{t}", cat='Binary'
                            )

            if not moderator_data:
                st.error("❌ No moderators with break entitlements were provided.")
                st.stop()

            # Every chronological position must contain exactly one break type at exactly one start time.
            for mod_id, info in moderator_data.items():
                for pos in info['Positions']:
                    pos_vars = [
                        var for (m, p, b, t), var in x.items()
                        if m == mod_id and p == pos
                    ]
                    if not pos_vars:
                        st.error(
                            f"❌ No valid time exists for break position {pos + 1} of {info['Name']} under the current rules."
                        )
                        st.stop()
                    prob += pulp.lpSum(pos_vars) == 1

                # Exact entitlement counts, while allowing arbitrary break-type order.
                for b_type in info['AllowedTypes']:
                    type_vars = [
                        var for (m, p, b, t), var in x.items()
                        if m == mod_id and b == b_type
                    ]
                    prob += pulp.lpSum(type_vars) == info['Counts'][b_type]

                # Build linear expressions for start and duration of each chronological position.
                for pos in info['Positions']:
                    vars_for_pos = [
                        (b, t, var) for (m, p, b, t), var in x.items()
                        if m == mod_id and p == pos
                    ]
                    position_start_expr[(mod_id, pos)] = pulp.lpSum(
                        t * var for b, t, var in vars_for_pos
                    )
                    position_duration_expr[(mod_id, pos)] = pulp.lpSum(
                        DURATIONS[b] * var for b, t, var in vars_for_pos
                    )

            # Chronological order + min/max inside-time constraints between consecutive breaks.
            # The position order is chronological, but BREAK TYPE is completely solver-selected.
            for mod_id, info in moderator_data.items():
                positions = info['Positions']

                for pos in positions[:-1]:
                    current_start = position_start_expr[(mod_id, pos)]
                    current_duration = position_duration_expr[(mod_id, pos)]
                    next_start = position_start_expr[(mod_id, pos + 1)]

                    prob += next_start >= current_start + current_duration + min_gap
                    prob += next_start <= current_start + current_duration + max_gap

                # Shift start -> first break
                first_start = position_start_expr[(mod_id, positions[0])]
                prob += first_start >= min_gap
                prob += first_start <= max_gap

                # Last break -> shift end
                last_pos = positions[-1]
                last_start = position_start_expr[(mod_id, last_pos)]
                last_duration = position_duration_expr[(mod_id, last_pos)]
                prob += total_shift_mins - (last_start + last_duration) >= min_gap
                prob += total_shift_mins - (last_start + last_duration) <= max_gap

            # Create flattening variables to mathematically penalize clustering.
            # Size dynamically so the model also works when >34 moderators are entered.
            max_possible_concurrency = max(1, len(moderator_data))
            e_vars = {}
            for t in time_intervals:
                for k in range(1, max_possible_concurrency + 1):
                    e_vars[(t, k)] = pulp.LpVariable(
                        f"e_{t}_{k}", lowBound=0, upBound=1, cat='Continuous'
                    )

            # Concurrency calculation directly from arbitrary-type start variables.
            for t in time_intervals:
                active_at_t = []
                active_wb70_at_t = []

                for (mod_id, pos, b_type, ts), var in x.items():
                    dur = DURATIONS[b_type]
                    if t - dur < ts <= t:
                        active_at_t.append(var)
                        if b_type == 'WB70':
                            active_wb70_at_t.append(var)

                # 1. Track absolute peak concurrency
                prob += pulp.lpSum(active_at_t) <= max_concurrent

                # 2. Specifically track WB70 concurrency to force staggering
                if active_wb70_at_t:
                    prob += pulp.lpSum(active_wb70_at_t) <= max_wb70_concurrent

                # 3. Connect active breaks to the flattening variables
                prob += pulp.lpSum(active_at_t) == pulp.lpSum(
                    e_vars[(t, k)] for k in range(1, max_possible_concurrency + 1)
                )

            # Objective:
            # - Weight 3000: strongly prevent 70-minute blocks from overlapping
            # - Weight 1000: minimize the overall maximum peak
            # - Weight k: flatten all remaining overlap across the schedule
            prob += (
                (3000 * max_wb70_concurrent)
                + (1000 * max_concurrent)
                + pulp.lpSum(
                    k * e_vars[(t, k)]
                    for t in time_intervals
                    for k in range(1, max_possible_concurrency + 1)
                )
            )

            # Same CBC settings as the original optimizer
            solver = pulp.PULP_CBC_CMD(timeLimit=60, msg=False, gapRel=0.05)
            status = prob.solve(solver)
            status_name = pulp.LpStatus[status]

            if status_name not in ['Optimal', 'Not Solved']:
                if prob.objective.value() is None:
                    st.error(
                        "❌ Conflicting Rules! The math is physically impossible with these exact gap/time constraints. "
                        "Try widening the Meal Window or increasing the Maximum Inside Time."
                    )
                    st.stop()

            # ==========================================
            # 6. EXTRACT & FORMAT DATA
            # ==========================================
            schedule = []
            selected_positions = set()

            for (mod_id, pos, b_type, t), var in x.items():
                value = pulp.value(var)
                if value is not None and value > 0.5:
                    start_dt = shift_start_dt + timedelta(minutes=t)
                    end_dt = start_dt + timedelta(minutes=DURATIONS[b_type])

                    duration_mins = DURATIONS[b_type]
                    start_str = start_dt.strftime('%H:%M')
                    end_str = end_dt.strftime('%H:%M')

                    if duration_mins <= 20:
                        bar_text = f"<b>{start_str}<br>{end_str}</b>"
                    else:
                        bar_text = f"<b>{start_str}-{end_str}</b>"

                    schedule.append(dict(
                        Task=f"<b>{moderator_data[mod_id]['Name']}</b>",
                        Resource=b_type,
                        Start=start_dt,
                        Finish=end_dt,
                        Bar_Text=bar_text,
                        _ModID=mod_id,
                        _Position=pos,
                    ))
                    selected_positions.add((mod_id, pos))

            expected_positions = sum(len(info['Positions']) for info in moderator_data.values())
            if not schedule or len(selected_positions) != expected_positions:
                st.error(
                    "❌ No complete feasible schedule was found within the solver run. "
                    "Try widening the Meal Window, increasing the Maximum Inside Time, or reducing conflicting fixed WB70 times."
                )
                st.stop()

            sched_df = pd.DataFrame(schedule)
            sched_df = sched_df.sort_values(by=['Task', 'Start'], ascending=[False, True])

            # Calculate Concurrency Over Time
            timeline_dts = [shift_start_dt + timedelta(minutes=t) for t in time_intervals]
            concurrency_counts = []
            for t_dt in timeline_dts:
                count = sum(1 for b in schedule if b['Start'] <= t_dt < b['Finish'])
                concurrency_counts.append(count)

            concurrency_df = pd.DataFrame({
                'Time': timeline_dts,
                'Concurrent Breaks': concurrency_counts
            })

            # ==========================================
            # 7. DASHBOARD & VISUALIZATION
            # ==========================================
            st.success(
                f"✅ Schedule Generated! Peak concurrent breaks held at: **{int(round(pulp.value(max_concurrent)))}**"
            )

            st.markdown(
                f"<div style='background-color: #1c2838; color: white; padding: 12px; border-radius: 4px; "
                f"text-align: center; font-size: 22px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
                f"Shift Break Timetable &bull; {shift_start_str}-{shift_end_str}</div>",
                unsafe_allow_html=True
            )
            st.markdown("<br>", unsafe_allow_html=True)

            color_map = {'Short': '#3b82f6', 'Meal': '#f97316', 'WB20': '#22c55e', 'WB70': '#a855f7'}

            # --- Gantt Chart ---
            fig_gantt = px.timeline(
                sched_df, x_start="Start", x_end="Finish", y="Task", color="Resource",
                text="Bar_Text", color_discrete_map=color_map
            )

            # Sharpen text and give bars crisp, distinct borders
            fig_gantt.update_traces(
                textposition='inside',
                insidetextanchor='middle',
                textangle=0,
                textfont=dict(family='Montserrat, sans-serif', color='white', size=11),
                marker=dict(line=dict(width=1, color='rgba(255, 255, 255, 0.6)'))
            )

            # Calculate dynamic height: 45 pixels per row + 150 pixels for padding/legend
            num_moderators = len(sched_df['Task'].unique())
            dynamic_height = max(600, num_moderators * 45 + 150)

            fig_gantt.update_layout(
                plot_bgcolor='white',
                paper_bgcolor='white',
                font=dict(family='Montserrat, sans-serif', color='black', size=12),
                xaxis=dict(
                    showgrid=True, gridcolor='#e5e5e5',
                    tickformat='%H:%M', dtick=3600000,
                    title="<b>Time</b>", side='bottom'
                ),
                yaxis=dict(
                    showgrid=True, gridcolor='#f3f4f6', title="",
                    tickfont=dict(color='#1c2838', size=12, family='Montserrat, sans-serif')
                ),
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="center",
                    x=0.5,
                    title=""
                ),
                margin=dict(l=0, r=0, t=60, b=40),
                height=dynamic_height
            )

            # Configure high-resolution image downloads (3x upscale = crystal-clear PNG)
            plotly_config = {
                'toImageButtonOptions': {
                    'format': 'png',
                    'filename': f'Break_Timetable_{shift_start_str}_{shift_end_str}',
                    'height': dynamic_height,
                    'width': 1800,
                    'scale': 3
                },
                'displayModeBar': True
            }

            st.plotly_chart(fig_gantt, use_container_width=True, config=plotly_config)

            # Direct High-Res PNG Download Button
            try:
                img_bytes = fig_gantt.to_image(format="png", width=1800, height=dynamic_height, scale=3)
                st.download_button(
                    label="📥 Download High-Resolution Timetable (PNG)",
                    data=img_bytes,
                    file_name=f"Timetable_{shift_start_str}_{shift_end_str}.png",
                    mime="image/png"
                )
            except Exception:
                st.info("💡 To enable the 1-click download button, ensure `kaleido` is added to your requirements.txt")

            # --- Concurrency Line Chart ---
            st.markdown(
                f"<div style='background-color: #1c2838; color: white; padding: 8px; border-radius: 4px; "
                f"text-align: center; font-size: 18px; font-weight: bold; font-family: Montserrat, sans-serif;'>"
                f"Concurrent Breaks Over Time</div>",
                unsafe_allow_html=True
            )
            st.markdown("<br>", unsafe_allow_html=True)

            fig_concurrency = px.area(
                concurrency_df, x='Time', y='Concurrent Breaks',
                color_discrete_sequence=['#3b82f6']
            )

            fig_concurrency.update_traces(line_shape='hv', fill='tozeroy', opacity=0.3)

            fig_concurrency.update_layout(
                plot_bgcolor='white',
                paper_bgcolor='white',
                font=dict(family='Montserrat, sans-serif', color='black', size=12),
                xaxis=dict(
                    showgrid=True, gridcolor='#e5e5e5',
                    tickformat='%H:%M', dtick=3600000,
                    title="<b>Time</b>"
                ),
                yaxis=dict(
                    showgrid=True, gridcolor='#f3f4f6', title="<b>Staff on Break</b>",
                    tickfont=dict(color='#1c2838', size=12, family='Montserrat, sans-serif'),
                    dtick=1
                ),
                margin=dict(l=0, r=0, t=20, b=40),
                height=300
            )

            st.plotly_chart(fig_concurrency, use_container_width=True)

        except Exception as e:
            st.error(f"An unexpected error occurred during scheduling calculation: {str(e)}")
