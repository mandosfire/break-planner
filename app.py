import streamlit as st
import pandas as pd
import plotly.express as px
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

shift_start_str = st.sidebar.text_input("Shift Start", value="23:30")
shift_end_str = st.sidebar.text_input("Shift End", value="08:00")
earliest_break_str = st.sidebar.text_input("Earliest Break Allowed", value="00:30")
final_break_str = st.sidebar.text_input("Final Break Must End By", value="07:15")

st.sidebar.markdown("---")
meal_start_str = st.sidebar.text_input("Meal Window Start", value="02:30")
meal_end_str = st.sidebar.text_input("Meal Window End", value="05:30")

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

# ==========================================
# 3. HELPER FUNCTIONS (TIME & SEQUENCING)
# ==========================================
def parse_time(time_str, base_date=datetime(2026, 1, 1)):
    if not time_str or pd.isna(time_str) or str(time_str).strip() == "":
        return None
    try:
        h, m = map(int, str(time_str).strip().split(':'))
        dt = base_date.replace(hour=h, minute=m, second=0)
        if h < 12:
            dt += timedelta(days=1)
        return dt
    except Exception:
        return None

def build_logical_sequence(shorts, meals, wb20s, wb70s):
    order = ['Short', 'Short', 'Meal', 'WB20', 'WB70', 'Short', 'Short', 'Short', 'Meal']
    seq = []
    counts = {'Short': shorts, 'Meal': meals, 'WB20': wb20s, 'WB70': wb70s}
    for b in order:
        if counts.get(b, 0) > 0:
            seq.append(b)
            counts[b] -= 1
    for b, count in counts.items():
        seq.extend([b]*count)
    return seq

# ==========================================
# 4. MODERATOR DATA TABLE
# ==========================================
st.subheader("Moderator List & Entitlements")

default_data = [
    {"Name": "Alper Uçar", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Arda Su Topcu", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Asiye Sağir", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Baki Doğan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Çağtay Kaplan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Damla Özçelik", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Ege Saritaş", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Ege Solaker", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "Meal Exception": True, "Fixed WB70 Start": ""},
    {"Name": "Gökay Deniz Akçayöz", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Gülsena Kaya", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Hilay Özgü Öztürk", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "İrem Kındıra", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "Meal Exception": True, "Fixed WB70 Start": ""},
    {"Name": "Kadirhan Tekin", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
    {"Name": "Saim Varol", "Shorts": 3, "Meals": 1, "WB20s": 0, "WB70s": 1, "Meal Exception": True, "Fixed WB70 Start": ""},
    {"Name": "Zeynep Öykü Ercan", "Shorts": 3, "Meals": 1, "WB20s": 1, "WB70s": 0, "Meal Exception": False, "Fixed WB70 Start": ""},
]

df = pd.DataFrame(default_data)
edited_df = st.data_editor(df, num_rows="dynamic", use_container_width=True)

# ==========================================
# 5. SOLVER ENGINE (PuLP)
# ==========================================
if st.button("🚀 Generate Optimized Schedule", type="primary"):
    with st.spinner("Calculating optimal break layout..."):
        try:
            base_dt = datetime(2026, 1, 1, 23, 30)
            shift_start_dt = parse_time(shift_start_str, base_dt)
            shift_end_dt = parse_time(shift_end_str, base_dt)
            earliest_dt = parse_time(earliest_break_str, base_dt)
            final_dt = parse_time(final_break_str, base_dt)
            meal_win_start = parse_time(meal_start_str, base_dt)
            meal_win_end = parse_time(meal_end_str, base_dt)

            total_shift_mins = int((shift_end_dt - shift_start_dt).total_seconds() / 60)
            time_intervals = list(range(0, total_shift_mins + 1, 5))

            prob = pulp.LpProblem("Maximize_On_Duty", pulp.LpMinimize)

            starts = {}
            seqs = {}
            max_concurrent = pulp.LpVariable("Max_Concurrent", lowBound=0, cat='Integer')

            for idx, row in edited_df.iterrows():
                mod = row['Name']
                if not mod: continue
                
                seq = build_logical_sequence(row['Shorts'], row['Meals'], row['WB20s'], row['WB70s'])
                seqs[mod] = seq
                
                for b_idx, b_type in enumerate(seq):
                    for t in time_intervals:
                        starts[(mod, b_idx, t)] = pulp.LpVariable(f"s_{mod}_{b_idx}_{t}", cat='Binary')

            for mod in seqs:
                for b_idx in range(len(seqs[mod])):
                    prob += pulp.lpSum([starts[(mod, b_idx, t)] for t in time_intervals]) == 1

            for mod in seqs:
                row = edited_df[edited_df['Name'] == mod].iloc[0]
                fixed_wb70_dt = parse_time(row['Fixed WB70 Start'], base_dt)
                
                for b_idx, b_type in enumerate(seqs[mod]):
                    dur = DURATIONS[b_type]
                    for t in time_intervals:
                        actual_time_dt = shift_start_dt + timedelta(minutes=t)
                        if actual_time_dt + timedelta(minutes=dur) > final_dt or actual_time_dt + timedelta(minutes=dur) > shift_end_dt:
                            prob += starts[(mod, b_idx, t)] == 0
                        if actual_time_dt < earliest_dt:
                            prob += starts[(mod, b_idx, t)] == 0
                        if b_type == 'Meal' and not row['Meal Exception']:
                            if actual_time_dt < meal_win_start or actual_time_dt + timedelta(minutes=dur) > meal_win_end:
                                prob += starts[(mod, b_idx, t)] == 0
                        if b_type == 'WB70' and fixed_wb70_dt is not None:
                            if actual_time_dt != fixed_wb70_dt:
                                prob += starts[(mod, b_idx, t)] == 0

            for mod in seqs:
                for b_idx in range(len(seqs[mod]) - 1):
                    dur = DURATIONS[seqs[mod][b_idx]]
                    current_start = pulp.lpSum([t * starts[(mod, b_idx, t)] for t in time_intervals])
                    next_start = pulp.lpSum([t * starts[(mod, b_idx+1, t)] for t in time_intervals])
                    prob += next_start >= current_start + dur + min_gap
                    prob += next_start <= current_start + dur + max_gap

            for mod in seqs:
                if len(seqs[mod]) > 0:
                    first_start = pulp.lpSum([t * starts[(mod, 0, t)] for t in time_intervals])
                    prob += first_start >= min_gap
                    prob += first_start <= max_gap
                    
                    last_idx = len(seqs[mod]) - 1
                    last_dur = DURATIONS[seqs[mod][last_idx]]
                    last_start = pulp.lpSum([t * starts[(mod, last_idx, t)] for t in time_intervals])
                    prob += total_shift_mins - (last_start + last_dur) >= min_gap
                    prob += total_shift_mins - (last_start + last_dur) <= max_gap

            for t in time_intervals:
                active_at_t = []
                for mod in seqs:
                    for b_idx, b_type in enumerate(seqs[mod]):
                        dur = DURATIONS[b_type]
                        start_window = [ts for ts in time_intervals if t - dur < ts <= t]
                        for ts in start_window:
                            active_at_t.append(starts[(mod, b_idx, ts)])
                prob += pulp.lpSum(active_at_t) <= max_concurrent
                
            prob += max_concurrent

            solver = pulp.PULP_CBC_CMD(timeLimit=30, msg=False)
            status = prob.solve(solver)

            if pulp.LpStatus[status] not in ['Optimal', 'Not Solved']: 
                if prob.objective.value() is None:
                    st.error("❌ Conflicting Rules! The math is physically impossible with these gap/time constraints. Try widening the Meal Window or Max Gap.")
                    st.stop()

            # ==========================================
            # 6. EXTRACT & FORMAT DATA
            # ==========================================
            schedule = []
            for mod in seqs:
                for b_idx, b_type in enumerate(seqs[mod]):
                    for t in time_intervals:
                        if pulp.value(starts[(mod, b_idx, t)]) == 1.0:
                            start_dt = shift_start_dt + timedelta(minutes=t)
                            end_dt = start_dt + timedelta(minutes=DURATIONS[b_type])
                            
                           # Determine label formatting based on duration to fit in the block
                            duration_mins = DURATIONS[b_type]
                            start_str = start_dt.strftime('%H:%M')
                            end_str = end_dt.strftime('%H:%M')
                            
                            # Add <b> tags for bold times
                            if duration_mins <= 20: 
                                bar_text = f"<b>{start_str}<br>{end_str}</b>"
                            else:
                                bar_text = f"<b>{start_str}-{end_str}</b>"
                                
                            schedule.append(dict(
                                Task=f"<b>{mod}</b>", # Add <b> tags for bold names
                                Resource=b_type, 
                                Start=start_dt, 
                                Finish=end_dt,
                                Bar_Text=bar_text
                            ))

            sched_df = pd.DataFrame(schedule)
            sched_df = sched_df.sort_values(by=['Task', 'Start'], ascending=[False, True])

            # ==========================================
            # 7. DASHBOARD & VISUALIZATION
            # ==========================================
            st.success(f"✅ Schedule Generated! Peak concurrent breaks held at: **{int(pulp.value(max_concurrent))}**")

            # Custom styled title block matching the image
            st.markdown(
                f"<div style='background-color: #1c2838; color: white; padding: 12px; border-radius: 4px; "
                f"text-align: center; font-size: 22px; font-weight: bold; font-family: sans-serif;'>"
                f"Shift Break Timetable &bull; {shift_start_str}-{shift_end_str}</div>", 
                unsafe_allow_html=True
            )
            st.markdown("<br>", unsafe_allow_html=True)

            color_map = {'Short': '#3b82f6', 'Meal': '#f97316', 'WB20': '#22c55e', 'WB70': '#a855f7'}
            
            fig = px.timeline(
                sched_df, x_start="Start", x_end="Finish", y="Task", color="Resource",
                text="Bar_Text", color_discrete_map=color_map
            )
            
         # Put text directly inside the bars and force it to remain horizontal
            fig.update_traces(
                textposition='inside', 
                insidetextanchor='middle',
                textangle=0,
                textfont=dict(family='Montserrat, sans-serif', color='white', size=10)
            )

           fig.update_layout(
                plot_bgcolor='white',
                paper_bgcolor='white',
                font=dict(family='Montserrat, sans-serif', color='black', size=12),
                xaxis=dict(
                    showgrid=True, gridcolor='#e5e5e5', 
                    tickformat='%H:%M', dtick=3600000, # 1 hour major tick lines
                    title="<b>Time</b>", side='bottom'
                ),
                yaxis=dict(
                    showgrid=True, gridcolor='#f3f4f6', title=""
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
                height=800
            )
            
            st.plotly_chart(fig, use_container_width=True)

        except Exception as e:
            st.error(f"An unexpected error occurred during scheduling calculation: {str(e)}")
