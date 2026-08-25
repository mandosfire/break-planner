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

# Dictionary for easy duration lookup
DURATIONS = {'Short': dur_short, 'Meal': dur_meal, 'WB20': dur_wb20, 'WB70': dur_wb70}

# ==========================================
# 3. HELPER FUNCTIONS (TIME & SEQUENCING)
# ==========================================
def parse_time(time_str, base_date=datetime(2026, 1, 1)):
    """Convert HH:MM string to datetime, handling overnight midnight crossing."""
    if not time_str or pd.isna(time_str) or str(time_str).strip() == "":
        return None
    try:
        h, m = map(int, str(time_str).strip().split(':'))
        dt = base_date.replace(hour=h, minute=m, second=0)
        # If time is small (e.g. 02:30) and shift starts at 23:30, it belongs to the next day
        if h < 12:
            dt += timedelta(days=1)
        return dt
    except Exception:
        return None

def build_logical_sequence(shorts, meals, wb20s, wb70s):
    """Automatically order breaks logically to help the solver."""
    order = ['Short', 'Short', 'Meal', 'WB20', 'WB70', 'Short', 'Short', 'Short', 'Meal']
    seq = []
    counts = {'Short': shorts, 'Meal': meals, 'WB20': wb20s, 'WB70': wb70s}
    
    for b in order:
        if counts.get(b, 0) > 0:
            seq.append(b)
            counts[b] -= 1
            
    # Catch any leftovers
    for b, count in counts.items():
        seq.extend([b]*count)
    return seq

# ==========================================
# 4. MODERATOR DATA TABLE
# ==========================================
st.subheader("Moderator List & Entitlements")

# Default data mimicking the user's prompt
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
            # Parse timeline limits
            base_dt = datetime(2026, 1, 1, 23, 30)
            shift_start_dt = parse_time(shift_start_str, base_dt)
            shift_end_dt = parse_time(shift_end_str, base_dt)
            earliest_dt = parse_time(earliest_break_str, base_dt)
            final_dt = parse_time(final_break_str, base_dt)
            meal_win_start = parse_time(meal_start_str, base_dt)
            meal_win_end = parse_time(meal_end_str, base_dt)

            total_shift_mins = int((shift_end_dt - shift_start_dt).total_seconds() / 60)
            time_intervals = list(range(0, total_shift_mins + 1, 5))

            # Initialize Model
            prob = pulp.LpProblem("Maximize_On_Duty", pulp.LpMinimize)

            # Variables
            starts = {}
            seqs = {}
            
            # The maximum simultaneous breaks at any given interval (we want to minimize this)
            max_concurrent = pulp.LpVariable("Max_Concurrent", lowBound=0, cat='Integer')

            for idx, row in edited_df.iterrows():
                mod = row['Name']
                if not mod: continue
                
                seq = build_logical_sequence(row['Shorts'], row['Meals'], row['WB20s'], row['WB70s'])
                seqs[mod] = seq
                
                for b_idx, b_type in enumerate(seq):
                    for t in time_intervals:
                        # starts[moderator, break_index, time]
                        starts[(mod, b_idx, t)] = pulp.LpVariable(f"s_{mod}_{b_idx}_{t}", cat='Binary')

            # 1. Exactly one start time per break
            for mod in seqs:
                for b_idx in range(len(seqs[mod])):
                    prob += pulp.lpSum([starts[(mod, b_idx, t)] for t in time_intervals]) == 1

            # 2. Hard constraints per break
            for mod in seqs:
                row = edited_df[edited_df['Name'] == mod].iloc[0]
                fixed_wb70_dt = parse_time(row['Fixed WB70 Start'], base_dt)
                
                for b_idx, b_type in enumerate(seqs[mod]):
                    dur = DURATIONS[b_type]
                    
                    for t in time_intervals:
                        actual_time_dt = shift_start_dt + timedelta(minutes=t)
                        
                        # Cannot start if it exceeds shift end or final break cutoff
                        if actual_time_dt + timedelta(minutes=dur) > final_dt or actual_time_dt + timedelta(minutes=dur) > shift_end_dt:
                            prob += starts[(mod, b_idx, t)] == 0
                            
                        # Earliest break allowed
                        if actual_time_dt < earliest_dt:
                            prob += starts[(mod, b_idx, t)] == 0

                        # Meal Window Constraints
                        if b_type == 'Meal' and not row['Meal Exception']:
                            if actual_time_dt < meal_win_start or actual_time_dt + timedelta(minutes=dur) > meal_win_end:
                                prob += starts[(mod, b_idx, t)] == 0
                                
                        # Fixed WB70 Start
                        if b_type == 'WB70' and fixed_wb70_dt is not None:
                            if actual_time_dt != fixed_wb70_dt:
                                prob += starts[(mod, b_idx, t)] == 0

            # 3. Inside Time (Gap) Constraints between sequential breaks
            for mod in seqs:
                for b_idx in range(len(seqs[mod]) - 1):
                    dur = DURATIONS[seqs[mod][b_idx]]
                    # start_time of next break - (start_time of current break + duration)
                    current_start = pulp.lpSum([t * starts[(mod, b_idx, t)] for t in time_intervals])
                    next_start = pulp.lpSum([t * starts[(mod, b_idx+1, t)] for t in time_intervals])
                    
                    # Min gap
                    prob += next_start >= current_start + dur + min_gap
                    # Max gap
                    prob += next_start <= current_start + dur + max_gap

            # 4. Shift Boundary Inside Times
            for mod in seqs:
                if len(seqs[mod]) > 0:
                    first_start = pulp.lpSum([t * starts[(mod, 0, t)] for t in time_intervals])
                    prob += first_start >= min_gap
                    prob += first_start <= max_gap
                    
                    last_idx = len(seqs[mod]) - 1
                    last_dur = DURATIONS[seqs[mod][last_idx]]
                    last_start = pulp.lpSum([t * starts[(mod, last_idx, t)] for t in time_intervals])
                    
                    # shift_end (total mins) - (last_start + last_dur)
                    prob += total_shift_mins - (last_start + last_dur) >= min_gap
                    prob += total_shift_mins - (last_start + last_dur) <= max_gap

            # 5. Objective: Minimize Peak Concurrency
            for t in time_intervals:
                active_at_t = []
                for mod in seqs:
                    for b_idx, b_type in enumerate(seqs[mod]):
                        dur = DURATIONS[b_type]
                        # A break is active at time 't' if it started between (t - dur + 5) and t
                        start_window = [ts for ts in time_intervals if t - dur < ts <= t]
                        for ts in start_window:
                            active_at_t.append(starts[(mod, b_idx, ts)])
                
                # The concurrent breaks at time t must be <= Max_Concurrent
                prob += pulp.lpSum(active_at_t) <= max_concurrent
                
            prob += max_concurrent

            # Solve (30-second time limit to ensure UI responsiveness)
            solver = pulp.PULP_CBC_CMD(timeLimit=30, msg=False)
            status = prob.solve(solver)

            if pulp.LpStatus[status] not in ['Optimal', 'Not Solved']: # CBC returns 'Not Solved' sometimes when time limits hit but solution exists
                if prob.objective.value() is None:
                    st.error("❌ Conflicting Rules! The math is physically impossible with these gap/time constraints. Try widening the Meal Window or Max Gap.")
                    st.stop()

            # ==========================================
            # 6. EXTRACT & FORMAT DATA
            # ==========================================
            schedule = []
            lark_blocks = {mod: [] for mod in seqs.keys()}

            for mod in seqs:
                for b_idx, b_type in enumerate(seqs[mod]):
                    for t in time_intervals:
                        if pulp.value(starts[(mod, b_idx, t)]) == 1.0:
                            start_dt = shift_start_dt + timedelta(minutes=t)
                            end_dt = start_dt + timedelta(minutes=DURATIONS[b_type])
                            schedule.append(dict(
                                Task=mod, 
                                Resource=b_type, 
                                Start=start_dt, 
                                Finish=end_dt
                            ))
                            lark_blocks[mod].append(f"{b_type}\t{start_dt.strftime('%H:%M')}")

            sched_df = pd.DataFrame(schedule)
            sched_df = sched_df.sort_values(by=['Task', 'Start'])

            # ==========================================
            # 7. DASHBOARD & VISUALIZATION
            # ==========================================
            st.success(f"✅ Schedule Generated! Peak concurrent breaks held at: **{int(pulp.value(max_concurrent))}**")

            color_map = {'Short': '#3182bd', 'Meal': '#e6550d', 'WB20': '#31a354', 'WB70': '#756bb1'}
            
            fig = px.timeline(sched_df, x_start="Start", x_end="Finish", y="Task", color="Resource",
                              color_discrete_map=color_map, title="Visual Break Timeline")
            
            fig.update_yaxes(autorange="reversed")
            fig.update_layout(
                plot_bgcolor='white',
                paper_bgcolor='white',
                font=dict(color='black'),
                xaxis=dict(
                    showgrid=True, gridcolor='lightgray', 
                    tickformat='%H:%M', dtick=1800000 # 30 min ticks
                )
            )
            st.plotly_chart(fig, use_container_width=True)

            # ==========================================
            # 8. LARK COPY-PASTE GENERATOR
            # ==========================================
            st.subheader("📋 Lark Paste Blocks")
            st.markdown("Copy these blocks directly into the corresponding Start columns in Lark.")
            
            # Determine maximum breaks any person took
            max_breaks = max([len(b) for b in lark_blocks.values()])
            
            cols = st.columns(max_breaks)
            for i in range(max_breaks):
                with cols[i]:
                    st.markdown(f"**Break {i+1}**")
                    block_text = ""
                    for mod in edited_df['Name']:
                        if mod in lark_blocks and i < len(lark_blocks[mod]):
                            block_text += lark_blocks[mod][i] + "\n"
                        else:
                            block_text += "\n"
                    st.text_area(label="hidden", value=block_text.strip(), height=350, label_visibility="collapsed")

        except Exception as e:
            st.error(f"An unexpected error occurred during scheduling calculation: {str(e)}")
