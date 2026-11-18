"""Lark Break Planner V11 — shared shift-roster driven optimization.
- Upload/edit a common versioned Google Sheets roster; no break-plan calendar or saving.
- Dates earlier than 2026-11-04 are always excluded.
- Fixed 3 Short + 1 Meal + 1 WB20/WB70; optional Ticket coverage.
- Export full-PU imagery, WFM/CSV data, and Lark Base clipboard TSV.
"""
from __future__ import annotations
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from functools import lru_cache
from io import BytesIO
import hashlib
import hmac
import json
import re
import uuid

import numpy as np
import pandas as pd
import streamlit as st

from roster import (
    FLOOR_DATE, PUS, PU_SHEETS, ROSTER_HEADERS,
    affected_conflicts, canonical_shift, connection_check,
    load_roster, normalize_name, parse_shift_code, parse_workbook,
    person_identity, publish_roster, read_excel_date, restore_roster_version,
    shift_label, validate_roster, list_roster_versions,
)
from wb70 import (
    parse_wb70_workbook, load_wb70_plan, publish_wb70_plan,
    list_wb70_versions, restore_wb70_version, match_wb70,
)
from optimizer import (
    BREAK_TYPES, TIME_STEP, build_candidate_patterns, build_pressure_profile,
    optimize_pattern_selection, pattern_vectors,
)
from exports import (
    generate_timetable_png, generate_heatmap_png, generate_csv,
    generate_wfm_shift_record_csv, name_for,
)
from exclusions import is_excluded_moderator
from lark_base_export import (
    build_lark_base_export, clipboard_button_html,
    new_batch_id, LarkExportError, _classify_shift,
)

st.set_page_config(page_title="Live Break Planner · V11.4.2", page_icon="🗓️",layout="wide")

st.markdown("""<style>
.block-container {padding-top: 1.2rem; max-width: 1700px;}
[data-testid="stMetric"] {background: #f1f6fa; border: 1px solid #d8e4ed; padding: 14px 18px; border-radius: 10px;}
</style>""",unsafe_allow_html=True)


def settings_secret(key,default=""):
    try:return st.secrets.get("break_planner",{}).get(key,default)
    except Exception:return default


def access_gate():
    access_password=str(settings_secret("access_password", "") or "")
    if not access_password:
        return
    if st.session_state.get("authenticated",False):return
    st.title("Live Break Planner — team access")
    code=st.text_input("Team access password",type="password")
    if st.button("Unlock planner",type="primary"):
        if hmac.compare_digest(code,access_password):
            st.session_state.authenticated=True
            st.rerun()
        else: st.error("Incorrect access password.")
    st.stop()


access_gate()

st.sidebar.title("LIVE BREAK PLANNER")
page=st.sidebar.radio("Workspace",["Break Planner","Shift Schedule Management","Weekly WB70 Upload","Storage & Help"],index=0)
st.sidebar.caption("Roster-driven · Break plans are never uploaded to the site")


@st.cache_data(ttl=25,show_spinner=False)
def cached_roster():
    return load_roster()

@st.cache_data(ttl=45,show_spinner=False)
def cached_wb70():
    return load_wb70_plan()


def load_remote_or_error():
    try:
        return cached_roster()
    except Exception as exc:
        st.error(f"Shared roster is unavailable: {exc}")
