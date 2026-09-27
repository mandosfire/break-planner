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

# ==========================================
# 1. PAGE CONFIGURATION & UI SETUP
# ==========================================
st.set_page_config(page_title="Lark Break Planner", layout="wide")
st.title("Shift Break Optimizer")
st.markdown(
    "Maximize on-duty staff while strictly enforcing meal windows, shift limits, "
    "inside-time rules, fixed WB70 times, per-moderator WB70 durations, moderator break entitlements, and queue-pressure-aware break placement."
)

# ==========================================
# 2. SIDEBAR RULES & CONFIGURATION
# ==========================================
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
