"""Lark Break Planner V11 — shared shift-roster driven optimization.
- Upload/edit a common versioned Google Sheets roster; no break-plan calendar or saving.
- Dates earlier than 2026-11-04 are always excluded.
- Fixed 3 Short + 1 Meal + 1 WB20/WB70; optional Ticket coverage.
- Export accessible full-PU image, heatmap, individual cards and CSV.
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
    affected_conflicts, canonical_shift, connection_check, load_moderator_settings,
    load_roster, normalize_name, parse_shift_code, parse_workbook,
    person_identity, publish_roster, read_excel_date, restore_roster_version,
    shift_label, upsert_moderator_settings, validate_roster, list_roster_versions,
)
from optimizer import (
    BREAK_TYPES, TIME_STEP, build_candidate_patterns, build_pressure_profile,
    optimize_pattern_selection, pattern_vectors,
)
from exports import (
    generate_timetable_png, generate_heatmap_png, generate_individual_card,
    generate_cards_zip, generate_csv, moderator_message, name_for,
)

st.set_page_config(page_title="Live Break Planner · V11.1", page_icon="🗓️",layout="wide")

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
        st.sidebar.warning("No access password configured. Protect the app's URL and roster data; optionally add [break_planner] access_password to Streamlit Secrets.")
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
page=st.sidebar.radio("Workspace",["Break Planner","Shift Schedule Management","Moderator Settings","Storage & Help"],index=0)
st.sidebar.caption("V11.1 · Roster-driven · Break plans are never uploaded to the site")


@st.cache_data(ttl=25,show_spinner=False)
def cached_roster():
    return load_roster()

@st.cache_data(ttl=45,show_spinner=False)
def cached_settings():
    return load_moderator_settings()


def load_remote_or_error():
    try:
        return cached_roster()
    except Exception as exc:
        st.error(f"Shared roster is unavailable: {exc}")
        st.info("Configure the existing private Google Sheets connection in Storage & Help. Generated break schedules are not saved in Google Sheets.")
        return [], {"snapshot_id":"","record_count":0}


def purge_caches():
    cached_roster.clear()
    cached_settings.clear()


def apply_draft_subset(all_rows,original_subset,edited):
    original_ids={str(x["record_id"]) for x in original_subset}
    merged=[dict(r) for r in all_rows if str(r.get("record_id")) not in original_ids]
    for _,entry in edited.iterrows():
        def cell_text(k):
            raw=entry.get(k, "")
            return "" if pd.isna(raw) else str(raw).strip()
        data={k:cell_text(k) for k in ("date","pu","name","email","wd_id","iex_id","shift_code","source_sheet","source_row","record_id")}
        if not data["name"] and not data["shift_code"]:
            continue
        if not data["record_id"]:data["record_id"]=uuid.uuid4().hex
        if not data["source_sheet"]:data["source_sheet"]="Manual correction"
        if not data["source_row"]:data["source_row"]="App"
        data["shift_code"]=canonical_shift(data["shift_code"]) or data["shift_code"].upper()
        merged.append(data)
    return sorted(merged,key=lambda x:(x.get("date", ""),x.get("pu", ""),x.get("name", "").casefold()))


def editor_frame(records):
    if not records:
        return pd.DataFrame(columns=["date","pu","name","email","wd_id","iex_id","shift_code","source_sheet","source_row","record_id"])
    return pd.DataFrame([{k:str(row.get(k, "") or "") for k in ("date","pu","name","email","wd_id","iex_id","shift_code","source_sheet","source_row","record_id")} for row in records])


def render_roster_editor(rows,label,button_text,key_prefix,on_apply):
    st.markdown(f"#### {label}")
    dates=sorted({r["date"] for r in rows if r.get("date") and read_excel_date(r.get("date")) and read_excel_date(r.get("date"))>=FLOOR_DATE})
    if not dates:
        st.info("No roster entries to edit.")
        return
    issues=validate_roster(rows)
