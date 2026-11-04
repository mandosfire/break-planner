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
st.sidebar.caption("Roster-driven · Break plans are never uploaded to the site")


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
    conflict_dates=sorted({i["Date"] for i in issues if i["Severity"]=="Error" and i["Date"] in dates})
    if conflict_dates:
        st.caption(f"Conflicting dates: {', '.join(conflict_dates[:16])}")
    cols=st.columns([2,2,1])
    with cols[0]:
        chosen=st.selectbox("Date to edit",dates,index=dates.index(conflict_dates[0]) if conflict_dates and conflict_dates[0] in dates else 0,key=key_prefix+"_date")
    with cols[1]:
        pu=st.selectbox("Power Unit",["All PUs"]+PUS,key=key_prefix+"_pu")
    with cols[2]:
        st.metric("Rows in selected view",sum(1 for r in rows if r.get("date")==chosen and (pu=="All PUs" or r.get("pu")==pu)))
    subset=[r for r in rows if r.get("date")==chosen and (pu=="All PUs" or r.get("pu")==pu)]
    columns={
        "date":st.column_config.TextColumn("Date YYYY-MM-DD",help="Hard cut-off: no earlier than 2026-11-04"),
        "pu":st.column_config.SelectboxColumn("PU",options=PUS,required=True),
        "name":st.column_config.TextColumn("Moderator name",required=True),
        "email":st.column_config.TextColumn("Email"),
        "wd_id":st.column_config.TextColumn("WD ID"),
        "iex_id":st.column_config.TextColumn("IEX ID"),
        "shift_code":st.column_config.TextColumn("Shift or status",help="Examples: 0730-1630, 1500-0000, 2330-0800, 0800-1700, OFF, ATT, ML"),
        "source_sheet":None,
        "source_row":None,
        "record_id":None,
    }
    st.caption("Edit shift times, move a moderator to another PU, set OFF, or delete a duplicate row. Changes are not shared until saved or published. Add rows with + at the bottom.")
    with st.form(key=f"{key_prefix}_editor_{chosen}_{pu}"):
        updated=st.data_editor(editor_frame(subset),num_rows="dynamic",width="stretch",hide_index=True,column_config=columns,disabled=["source_sheet","source_row","record_id"],key=f"{key_prefix}_grid_{chosen}_{pu}")
        apply=st.form_submit_button(button_text,type="primary")
    if apply:
        new_rows=apply_draft_subset(rows,subset,updated)
        invalids=[i for i in validate_roster(new_rows) if i["Severity"]=="Error" and (i["Issue"] in ("Invalid/out-of-scope date","Unrecognized Power Unit","Missing moderator name"))]
        if invalids:
            st.error(f"Fix invalid date, PU or name values first. First issue: {invalids[0]}")
        else:
            on_apply(new_rows)


def render_issues(rows,heading="Roster validation"):
    issues=validate_roster(rows)
    blockers=[i for i in issues if i["Severity"]=="Error"]
    notices=[i for i in issues if i["Severity"]=="Warning"]
    st.markdown(f"### {heading}")
    if not issues:
        st.success("No conflicts or data-quality issues found.")
    else:
        a,b=st.columns(2)
        with a: st.metric("Active assignment conflicts/errors",len(blockers))
        with b: st.metric("Other warnings",len(notices))
        st.dataframe(pd.DataFrame(issues),width="stretch",hide_index=True,height=min(520,65+len(issues)*37))
        if blockers: st.warning("Active duplicates can be published for shared review, but affected shifts are BLOCKED from break generation until corrected.")
    return issues


def date_options(rows):
    return sorted({str(r["date"]) for r in rows if str(r.get("date", ""))>="2026-11-04"})


def merged_upload(existing,new_rows,report):
    cover=set()
    for entry in report:
        if not entry["Dates"]:continue
        start=date.fromisoformat(entry["First date"]);end=date.fromisoformat(entry["Last date"])
        day=start
        while day<=end:
            cover.add((entry["PU"],day.isoformat()))
            day+=timedelta(days=1)
    remaining=[dict(r) for r in existing if (r.get("pu"),r.get("date")) not in cover]
    return sorted(remaining+new_rows,key=lambda r:(r.get("date", ""),r.get("pu", ""),r.get("name", "").casefold()))


def schedule_management():
    st.title("Shift Schedule Management")
    st.caption("Five fixed source worksheets → Agent / Oncall / HLC / KU / AZ. Only 2026-11-04 onward is imported. Edits and imports are shared through Google Sheets.")
    with st.expander("Official Power Unit worksheet mapping",expanded=False):
        st.table(pd.DataFrame([{"Excel worksheet":k,"Power Unit":v} for k,v in PU_SHEETS.items()]))
    current,meta=load_remote_or_error()
    if current:
        st.success(f"Shared roster: {len(current):,} assignments · snapshot {str(meta.get('snapshot_id',''))[:12]} · last published {meta.get('created_at','—')}")
    else:
        st.info("No shared roster is published yet. Upload a workbook, review it, then publish.")
    st.divider()
    st.markdown("### Upload revised operation shift schedule")
    uploader=st.text_input("Uploaded / edited by",key="roster_uploader",placeholder="WFM name or initials")
    uploaded=st.file_uploader("Upload the Live Shift Schedule Excel file (.xlsx)",type=["xlsx"],key="roster_file")
    if uploaded:
        filebytes=uploaded.getvalue()
        fingerprint=hashlib.sha256(filebytes).hexdigest()
        if fingerprint==st.session_state.get("last_published_upload_fingerprint") and "draft_roster" not in st.session_state:
            st.caption("This exact workbook has already been published during this browser session.")
            if st.button("Re-import this same workbook for another correction",key="reimport_same_file"):
                st.session_state.pop("last_published_upload_fingerprint",None)
                st.rerun()
        if fingerprint not in (st.session_state.get("uploaded_fingerprint"),st.session_state.get("last_published_upload_fingerprint")):
            try:
                imported,report=parse_workbook(filebytes)
                st.session_state["draft_roster"]=merged_upload(current,imported,report)
                st.session_state["upload_report"]=report
                st.session_state["uploaded_fingerprint"]=fingerprint
                st.session_state["draft_based_on"]=str(meta.get("snapshot_id",""))
                st.session_state["draft_uploaded_name"]=uploaded.name
            except Exception as exc:
                st.error(f"Import failed: {exc}")
    if "draft_roster" in st.session_state:
        draft=st.session_state["draft_roster"]
        st.markdown("#### Import preview")
        if st.session_state.get("upload_report"):
            st.dataframe(pd.DataFrame(st.session_state["upload_report"]),width="stretch",hide_index=True)
        st.metric("Total assignments in proposed shared roster",len(draft))
        issues=render_issues(draft,"Draft validation")
        def apply_draft(merged):
            st.session_state["draft_roster"]=merged
            st.success("Draft edits applied. Other dates/PUs are preserved. Publish when ready.")
            st.rerun()
        render_roster_editor(draft,"Edit uploaded draft before publishing","Apply draft edits","draft",apply_draft)
        errs=sum(i["Severity"]=="Error" for i in issues)
        acknowledge=True
        if errs:
            acknowledge=st.checkbox("I understand unresolved active duplicates will block break generation for affected shifts.",key="draft_acknowledge")
        if st.button("Publish this roster for the whole team",type="primary",disabled=not acknowledge,key="publish_draft"):
            try:
                saved=publish_roster(draft,uploader,description=f"Excel import: {st.session_state.get('draft_uploaded_name','shift schedule')}",expected_snapshot=st.session_state.get("draft_based_on"))
                st.session_state["last_published_upload_fingerprint"] = st.session_state.get("uploaded_fingerprint")
                for field in ("draft_roster","upload_report","uploaded_fingerprint","draft_based_on","draft_uploaded_name"):
                    st.session_state.pop(field,None)
                purge_caches()
                st.success(f"Published {saved['record_count']:,} roster assignments for every website user.")
                st.rerun()
            except Exception as exc: st.error(f"Cannot publish: {exc}")
    st.divider()
    if current:
        render_issues(current,"Current shared roster validation")
        def apply_live(merged):
            try:
                saved=publish_roster(merged,uploader,description="Correction in shared roster editor",expected_snapshot=str(meta.get("snapshot_id", "")))
                purge_caches()
                st.success(f"Saved and published new roster revision {saved['snapshot_id'][:12]} ({len(merged):,} assignments).")
                st.rerun()
            except Exception as exc:st.error(f"Could not save roster edits: {exc}")
        render_roster_editor(current,"Edit current shared roster","Save roster corrections","live",apply_live)
        with st.expander("Roster revision history / restore",expanded=False):
            st.caption("The app never deletes previously published roster snapshots. Restoring a snapshot changes which roster is active; it does NOT restore or save break plans.")
            try:
                versions=list_roster_versions()
                if versions:
                    st.dataframe(pd.DataFrame(versions).tail(20),width="stretch",hide_index=True)
                    choices=[str(v["snapshot_id"]) for v in reversed(versions)]
                    restore_id=st.selectbox("Choose roster version",choices,format_func=lambda sid:next((f"{v['created_at']} · {v['uploaded_by']} · {v['record_count']} assignments ({sid[:10]})" for v in versions if str(v['snapshot_id'])==sid),sid))
                    if st.button("Restore selected shared roster version"):
                        if not uploader.strip():st.error("Enter uploader name above to restore.")
                        else:
                            restore_roster_version(restore_id,uploader)
                            purge_caches();st.rerun()
            except Exception as exc:st.error(f"Cannot load revisions: {exc}")


def settings_management():
    st.title("Persistent Moderator Settings")
    st.caption("Remember WB70 and Ticket Moderator defaults across future uploads. Break preparation can override these settings for a single shift without changing the saved defaults.")
    roster,meta=load_remote_or_error()
    if not roster:return
    try: saved=cached_settings()
    except Exception as exc:
        st.error(f"Cannot load moderator settings: {exc}")
        return
    pu=st.selectbox("Power Unit",PUS,key="settings_pu")
    # Include all recognized staff in this PU, including staff who are OFF today.
    unique={}
    for r in roster:
        if r["pu"]!=pu:continue
        identity=person_identity(r)
        if identity and r["name"]:unique[identity]=r["name"]
    data=[]
    for identity,name in sorted(unique.items(),key=lambda t:t[1].casefold()):
        opts=saved.get((pu,identity),{})
        data.append({"Identity":identity,"Name":name,"WB70":bool(opts.get("wb70",False)),"Ticket Moderator":bool(opts.get("ticket",False)),"WB70 Duration":opts.get("wb70_duration", ""),"Fixed WB70 Start":opts.get("fixed_wb70", "")})
    st.caption(f"{len(data)} moderators found for {pu}. Unchecked WB70 means WB20. Duration/fixed start fields are optional.")
    with st.form("settings_form"):
        edited=st.data_editor(pd.DataFrame(data),width="stretch",hide_index=True,disabled=["Identity","Name"],column_config={"Identity":None,"WB70":st.column_config.CheckboxColumn("WB70?"),"Ticket Moderator":st.column_config.CheckboxColumn("Ticket?"),"WB70 Duration":st.column_config.TextColumn("WB70 duration (mins)"),"Fixed WB70 Start":st.column_config.TextColumn("Fixed WB70 start HH:MM")},key="settings_editor_"+pu)
        save=st.form_submit_button("Save defaults for this Power Unit",type="primary")
    if save:
        records=[];errors=[]
        for _,r in edited.iterrows():
            duration=str(r["WB70 Duration"] or "").strip()
            fixed=str(r["Fixed WB70 Start"] or "").strip()
            if duration and (not duration.isdigit() or int(duration)<5 or int(duration)%5!=0):errors.append(f"{r['Name']}: duration must be a positive 5-minute multiple")
            if fixed and not re.fullmatch(r"\d{1,2}:\d{2}",fixed):errors.append(f"{r['Name']}: fixed time must be HH:MM")
            records.append({"identity":r["Identity"],"name":r["Name"],"wb70":bool(r["WB70"]),"ticket":bool(r["Ticket Moderator"]),"wb70_duration":duration,"fixed_wb70":fixed})
        if errors:st.error("; ".join(errors[:6]))
        else:
            try:
                n=upsert_moderator_settings(pu,records);cached_settings.clear()
                st.success(f"Saved persistent defaults for {n} moderators in {pu}.")
            except Exception as exc:st.error(f"Could not save settings: {exc}")


def shift_rule_defaults(selected_date,code):
    times=parse_shift_code(code)
    if not times:raise ValueError(f"Invalid shift: {code}")
    sh,eh=times
    start=datetime.combine(date.fromisoformat(selected_date),time.fromisoformat(sh))
    end=datetime.combine(date.fromisoformat(selected_date),time.fromisoformat(eh))
    if end<=start:end+=timedelta(days=1)
    canonical=canonical_shift(code)
    preset="Morning" if canonical=="0730-1630" else "Mid" if canonical=="1500-0000" else "Night" if canonical=="2330-0800" else "Custom"
    pressure_class=preset
    if preset=="Custom" and 5<=start.hour<12 and end.hour<=20:pressure_class="Morning"
    if preset=="Custom" and 14<=start.hour<20:pressure_class="Mid"
    earliest=start+timedelta(minutes=60)
    final=end-timedelta(minutes=45)
    if preset=="Morning":meal_start=start.replace(hour=12,minute=0);meal_end=start.replace(hour=14,minute=30)
    elif preset=="Mid":meal_start=start.replace(hour=17,minute=0);meal_end=start.replace(hour=21,minute=0)
    elif preset=="Night":meal_start=start+timedelta(hours=2,minutes=30);meal_end=start+timedelta(hours=5,minutes=30)
    elif 5<=start.hour<=10:
        meal_start=start.replace(hour=12,minute=0);meal_end=start.replace(hour=14,minute=30)
    else:
        meal_start=start+timedelta(hours=3);meal_end=start+timedelta(hours=6)
    return {"start":start,"end":end,"earliest":earliest,"final":final,"meal_start":meal_start,"meal_end":meal_end,"preset":preset,"pressure_class":pressure_class}


def resolve_clock(clock,shift_start):
    try:
        hh,mm=map(int,str(clock).strip().split(":"))
        if not 0<=hh<=23 or not 0<=mm<=59:raise ValueError
        dt=shift_start.replace(hour=hh,minute=mm)
        if dt<shift_start:dt+=timedelta(days=1)
        return dt
    except Exception as exc:
        raise ValueError(f"Time '{clock}' is invalid; please use HH:MM.") from exc


def compute_breaks(selected_date,pu,code,chosen_rows,rule):
    """Optimize from the current selected roster, strictly validate all hard constraints."""
    defaults=shift_rule_defaults(selected_date,code)
    start=defaults["start"];end=defaults["end"]
    earliest=resolve_clock(rule["earliest"],start)
    final=resolve_clock(rule["final"],start)
    meal_start=resolve_clock(rule["meal_start"],start)
    meal_end=resolve_clock(rule["meal_end"],start)
    if not (start<earliest<final<end):raise ValueError("Break-window boundaries must lie strictly inside the shift in chronological order.")
    if meal_end<=meal_start:raise ValueError("Meal window end must follow its start.")
    min_gap,max_gap=rule["min_gap"],rule["max_gap"]
    if not (0<=min_gap<=max_gap):raise ValueError("Minimum inside time cannot exceed maximum inside time.")
    if int((end-start).total_seconds()/60)>780:raise ValueError("Shifts longer than 13 hours are not supported.")
    total=int((end-start).total_seconds()/60)
    if total%5:raise ValueError("Shift duration must be divisible into 5-minute intervals.")
    moderators=[];profile_cache={}
    active_count=sum(bool(row.get("Include",False)) for row in chosen_rows)
    candidate_cap=min(300,max(90,3600//max(1,active_count)))
    for row in chosen_rows:
        if not bool(row["Include"]):continue
        name=str(row["Name"])
        wb70=bool(row["WB70"])
        counts={"Short":3,"Meal":1,"WB20":0 if wb70 else 1,"WB70":1 if wb70 else 0}
        dur=dict(rule["durations"])
        custom_duration=str(row.get("WB70 Duration (mins)", "") or "").strip()
        if wb70 and custom_duration:
            if not custom_duration.isdigit() or int(custom_duration)%5 or int(custom_duration)<5:
                raise ValueError(f"{name}: WB70 duration must be a positive multiple of 5 minutes.")
            dur["WB70"]=int(custom_duration)
        fixed_clock=str(row.get("Fixed WB70 Start", "") or "").strip()
        if fixed_clock and not wb70:raise ValueError(f"{name}: Fixed WB70 requires WB70 checkbox to be checked.")
        fixed_mins=None
        if fixed_clock:
            fixed_dt=resolve_clock(fixed_clock,start)
            fixed_mins=int((fixed_dt-start).total_seconds()/60)
            if fixed_mins%5:raise ValueError(f"{name}: Fixed WB70 must be on a five-minute boundary.")
        profile=(tuple((b,counts[b]) for b in BREAK_TYPES),tuple((b,dur[b]) for b in BREAK_TYPES),fixed_mins,bool(rule["allow_wb70_second_half"]))
        if profile not in profile_cache:
            patterns=build_candidate_patterns(
                counts=counts,durations=dur,total_shift_mins=total,
                earliest_mins=int((earliest-start).total_seconds()/60),
                final_mins=int((final-start).total_seconds()/60),
                meal_start_mins=int((meal_start-start).total_seconds()/60),
                meal_end_mins=int((meal_end-start).total_seconds()/60),
                min_inside=min_gap,max_inside=max_gap,fixed_wb70_mins=fixed_mins,
                allow_wb70_second_half=bool(rule["allow_wb70_second_half"]),
                max_patterns=candidate_cap,
            )
            if not patterns:
                raise ValueError(f"{name}: no feasible five-break layout for this shift, WB setting and inside-time rules. Check fixed WB70, meal window or first-half restriction.")
            profile_cache[profile]=patterns
        moderators.append({"Name":name,"Counts":counts,"Durations":dur,"TicketModerator":bool(row["Ticket Moderator"]),"Profile":profile})
    if not moderators:raise ValueError("No moderators are included in the plan.")
    tickets=[m for m in moderators if m["TicketModerator"]]
    if len(tickets)==1:raise ValueError(f"Only one Ticket Moderator is selected ({tickets[0]['Name']}). Mark a second Ticket Moderator or uncheck the first; a single person cannot cover tickets while on break.")
    timeline=list(range(0,total+1,TIME_STEP))
    weights=build_pressure_profile(defaults["pressure_class"],start,timeline)["Weight"]
    pattern_sets=[];vector_sets=[];vector_cache={}
    for mod in moderators:
        patterns=profile_cache[mod["Profile"]]
        pattern_sets.append(patterns)
        if mod["Profile"] not in vector_cache:
            cols=[pattern_vectors(p,mod["Durations"],timeline) for p in patterns]
            vector_cache[mod["Profile"]]=(np.stack([a for a,w in cols],axis=1),np.stack([w for a,w in cols],axis=1))
        vector_sets.append(vector_cache[mod["Profile"]])
    result=optimize_pattern_selection(moderators,pattern_sets,vector_sets,timeline,weights)
    schedule=[]
    for i,mod in enumerate(moderators):
        pattern=pattern_sets[i][result["Chosen"][i]]
        for kind,minutes in zip(pattern["Order"],pattern["Starts"]):
            t=start+timedelta(minutes=int(minutes));finish=t+timedelta(minutes=mod["Durations"][kind])
            schedule.append({"Name":mod["Name"],"Resource":kind,"Start":t,"Finish":finish,"TicketModerator":mod["TicketModerator"]})
    # Independent hard-constraint audit before exposing or exporting the schedule.
    for mod in moderators:
        seq=sorted([b for b in schedule if b["Name"]==mod["Name"]],key=lambda b:b["Start"])
        if len(seq)!=5 or Counter(b["Resource"] for b in seq)!={k:v for k,v in mod["Counts"].items() if v}:
            raise RuntimeError(f"Internal entitlement-validation error for {mod['Name']}")
        if seq[0]["Resource"]=="Meal":raise RuntimeError(f"Meal-first violation for {mod['Name']}")
        times=[(start,seq[0]["Start"]),(seq[-1]["Finish"],end)]+[(a["Finish"],b["Start"]) for a,b in zip(seq,seq[1:])]
        for a,b in times:
            gap=(b-a).total_seconds()/60
            if gap<min_gap-0.01 or gap>max_gap+0.01:raise RuntimeError(f"Inside-gap validation failure for {mod['Name']}")
    if len(tickets)>=2:
        ticket_names={m["Name"] for m in tickets}
        for t in timeline:
            now=start+timedelta(minutes=t)
            if sum(b["Name"] in ticket_names and b["Start"]<=now<b["Finish"] for b in schedule)>=len(tickets):
                raise RuntimeError("Internal Ticket Moderator coverage validation failed")
    return {
        "date":selected_date,"pu":pu,"shift_code":canonical_shift(code),"shift_label":shift_label(code),
        "shift_start_dt":start,"shift_end_dt":end,"earliest_dt":earliest,"final_dt":final,
        "schedule":schedule,"peak_concurrent":max(sum(b["Start"]<=start+timedelta(minutes=t)<b["Finish"] for b in schedule) for t in timeline),
        "peak_wb70":max(sum(b["Resource"]=="WB70" and b["Start"]<=start+timedelta(minutes=t)<b["Finish"] for b in schedule) for t in timeline),
        "tickets":len(tickets),"used_fallback":bool(result["UsedFallback"]),"allow_wb70_second_half":bool(rule["allow_wb70_second_half"]),
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "pressure_summary":pd.DataFrame({"Time":[start+timedelta(minutes=t) for t in timeline],"Pressure Weight":weights}),
    }


def display_generated(payload):
    st.markdown("### Generated break schedule")
    info=st.columns(4)
    with info[0]:st.metric("Moderators",len({b["Name"] for b in payload["schedule"]}))
    with info[1]:st.metric("Peak concurrent breaks",payload["peak_concurrent"])
    with info[2]:st.metric("Peak simultaneous WB70s",payload["peak_wb70"])
    with info[3]:st.metric("Ticket moderators",payload["tickets"])
    if payload["used_fallback"]:st.warning("Global optimizer did not converge to a MILP solution in time, so the fully feasible ticket-safe fallback was used.")
    st.caption("All five breaks validated per included moderator. Meal is never first. Fixed WB70 overrides the optional first-half limit. Your generated plan is kept only in this browser session and is not uploaded to the site.")
    cache=st.session_state.get("result_exports")
    if not cache or cache.get("timestamp") != payload["generated_at"]:
        cache={"timestamp":payload["generated_at"]}
        st.session_state["result_exports"]=cache
    for export_kind, build_export in (
        ("timetable", generate_timetable_png),
        ("heatmap", generate_heatmap_png),
    ):
        if export_kind not in cache:
            with st.spinner(f"Rendering {export_kind} PNG..."):
                try:
                    cache[export_kind]=build_export(payload)
                    cache.pop(export_kind + "_error", None)
                except Exception as exc:
                    cache[export_kind + "_error"]=str(exc)
    st.markdown("#### Full Power Unit timetable")
    if cache.get("timetable"):
        st.image(cache["timetable"],width="stretch")
        st.download_button("Download full PU timetable PNG",data=cache["timetable"],file_name=name_for("Timetable",payload),mime="image/png",type="primary")
    else:
        st.error(f"Timetable PNG could not be rendered: {cache.get('timetable_error', 'Unknown error')}")
    st.markdown("#### Concurrent-break heatmap")
    if cache.get("heatmap"):
        st.image(cache["heatmap"],width="stretch")
        st.download_button("Download 5-minute heatmap PNG",data=cache["heatmap"],file_name=name_for("Concurrent_Breaks",payload),mime="image/png")
    else:
        st.error(f"Heatmap PNG could not be rendered: {cache.get('heatmap_error', 'Unknown error')}")
    if cache.get("timetable_error") or cache.get("heatmap_error"):
        if st.button("Retry PNG rendering",key="retry_png_render"):
            st.session_state.pop("result_exports",None)
            st.rerun()
    with st.expander("Exact break times — searchable",expanded=False):
        display=pd.DataFrame([{"Moderator":b["Name"],"Break":b["Resource"],"Start":b["Start"].strftime("%H:%M"),"End":b["Finish"].strftime("%H:%M"),"Ticket":b["TicketModerator"]} for b in sorted(payload["schedule"],key=lambda x:(x["Name"].casefold(),x["Start"]))])
        st.dataframe(display,width="stretch",hide_index=True)
        st.download_button("Download break times CSV",data=generate_csv(payload),file_name=name_for("Break_Times",payload,"csv"),mime="text/csv")
    st.markdown("#### Individual moderator communication")
    names=sorted({b["Name"] for b in payload["schedule"]},key=str.casefold)
    chosen=st.selectbox("Find moderator",names,key="individual_moderator")
    text=moderator_message(payload,chosen)
    st.code(text,language=None)
    st.caption("Use the copy icon above to send the moderator their five exact break times in your existing PU or individual chat. Moderators do not need access to the website.")
    all_break_summaries = "\n\n".join(
        moderator_message(payload, name) for name in names
    ) + "\n"
    st.download_button(
        "Download all moderator break summaries (TXT)",
        data=all_break_summaries.encode("utf-8-sig"),
        file_name=name_for("Moderator_Break_Summaries", payload, "txt"),
        mime="text/plain",
    )
    individual_cache=st.session_state.get("individual_png_cache",{})
    if individual_cache.get("timestamp") != payload["generated_at"]:
        individual_cache={"timestamp":payload["generated_at"],"images":{}}
        st.session_state["individual_png_cache"]=individual_cache
    if st.button("Prepare this moderator's PNG",key="prepare_single_card"):
        with st.spinner("Rendering this moderator's break card..."):
            try:
                individual_cache["images"][chosen]=generate_individual_card(payload,chosen)
            except Exception as exc:
                st.error(f"Could not render individual PNG: {exc}")
    if chosen in individual_cache["images"]:
        st.download_button(
            "Download this moderator's break card PNG",
            data=individual_cache["images"][chosen],
            file_name=f"Individual_{payload['date']}_{payload['pu']}_{re.sub(r'[^\w-]+','_',chosen)}.png",
            mime="image/png",
        )
    st.caption("For a large PU, preparing the complete PNG ZIP can take some time. Use the direct TXT download above when images are unnecessary.")
    cards_zip_cache=st.session_state.get("individual_zip_cache",{})
    if cards_zip_cache.get("timestamp") != payload["generated_at"]:
        cards_zip_cache={"timestamp":payload["generated_at"]}
        st.session_state["individual_zip_cache"]=cards_zip_cache
    if st.button("Prepare all individual break cards (ZIP)",key="prepare_all_cards"):
        with st.spinner("Rendering individual cards and packaging ZIP..."):
            try:
                cards_zip_cache["data"]=generate_cards_zip(payload)
            except Exception as exc:
                st.error(f"Could not prepare ZIP: {exc}")
    if cards_zip_cache.get("data"):
        st.download_button(
            "Download all individual break cards (ZIP)",
            data=cards_zip_cache["data"],
            file_name=name_for("Individual_Cards",payload,"zip"),
            mime="application/zip",
        )
    with st.expander("Operational pressure used for optimization"):
        st.line_chart(payload["pressure_summary"].set_index("Time"),height=230)
        st.caption("Night volume is treated as uniform. Morning/Mid overlap receives relative pressure 0.50 between 15:00–16:30.")


def break_planner():
    st.title("Generate breaks from the shared shift roster")
    st.caption("Choose Date + PU + Shift. Moderator names come from the latest published shift schedule; no manual list entry required.")
    roster,meta=load_remote_or_error()
    if not roster:
        st.warning("Upload and publish a roster on the Shift Schedule Management page first.")
        return
    dates=date_options(roster)
    if not dates:
        st.error("No roster dates on/after 2026-11-04.")
        return
    today=date.today().isoformat()
    default_date=next((day for day in dates if day>=today),dates[-1])
    cols=st.columns([1.4,1.2,2])
    with cols[0]:chosen_date=st.selectbox("Date",dates,index=dates.index(default_date),key="planner_date")
    pu_options=[pu for pu in PUS if any(r["date"]==chosen_date and r["pu"]==pu for r in roster)]
    with cols[1]:pu=st.selectbox("Power Unit",pu_options,key="planner_pu") if pu_options else None
    if not pu:
        st.info("No Power Units available for the selected date.")
        return
    shift_counts=Counter(canonical_shift(r["shift_code"]) for r in roster if r["date"]==chosen_date and r["pu"]==pu and parse_shift_code(r["shift_code"]))
    shifts=sorted([code for code in shift_counts if code],key=lambda x:x[:4])
    with cols[2]:code=st.selectbox("Scheduled shift",shifts,format_func=lambda c:f"{shift_label(c)} ({shift_counts[c]} moderators)",key="planner_shift") if shifts else None
    if not code:
        st.warning("No timed shifts found for this selected date and Power Unit. Check the roster status codes.")
        return
    conflicting=affected_conflicts(roster,chosen_date,pu,code)
    if conflicting:
        st.error("ROSTER CONFLICT — This shift contains an active moderator also assigned to another PU or twice in the same PU on this date. Fix the highlighted assignments in Shift Schedule Management before generating.")
        st.dataframe(pd.DataFrame(conflicting),width="stretch",hide_index=True)
        return
    rows=[r for r in roster if r["date"]==chosen_date and r["pu"]==pu and canonical_shift(r["shift_code"])==code]
    try:saved=cached_settings()
    except Exception as exc:
        st.warning(f"Moderator defaults could not load; using WB20/ticket-off defaults: {exc}")
        saved={}
    choices=[]
    for r in sorted(rows,key=lambda x:x["name"].casefold()):
        opts=saved.get((pu,person_identity(r)),{})
        choices.append({"Name":r["name"],"Include":True,"WB70":bool(opts.get("wb70",False)),"Ticket Moderator":bool(opts.get("ticket",False)),"Fixed WB70 Start":str(opts.get("fixed_wb70", "") or ""),"WB70 Duration (mins)":str(opts.get("wb70_duration", "") or ""),"Identity":person_identity(r)})
    st.markdown(f"### {len(choices)} moderators · {pu} · {shift_label(code)}")
    st.caption("Automatic entitlements for every included moderator: 3 Shorts, 1 Meal, 1 Wellness. Check WB70 for 70-minute wellness; unchecked means WB20. Ticket coverage never allows all checked ticket moderators to be on break together. Use Include to exclude a temporary absence without changing the shared roster.")
    edited=st.data_editor(pd.DataFrame(choices),width="stretch",hide_index=True,disabled=["Name","Identity"],num_rows="fixed",column_config={"Identity":None,"Include":st.column_config.CheckboxColumn("Include"),"WB70":st.column_config.CheckboxColumn("WB70?"),"Ticket Moderator":st.column_config.CheckboxColumn("Ticket?"),"Fixed WB70 Start":st.column_config.TextColumn("Fixed WB70 HH:MM"),"WB70 Duration (mins)":st.column_config.TextColumn("WB70 duration (optional)")},key=f"plan_edit_{chosen_date}_{pu}_{code}_{meta.get('snapshot_id','')}")
    if st.button("Save these WB70/Ticket choices as permanent PU defaults"):
        updates=[{"identity":r["Identity"],"name":r["Name"],"wb70":bool(r["WB70"]),"ticket":bool(r["Ticket Moderator"]),"wb70_duration":str(r["WB70 Duration (mins)" ] or ""),"fixed_wb70":str(r["Fixed WB70 Start"] or "")} for _,r in edited.iterrows()]
        try:
            n=upsert_moderator_settings(pu,updates);cached_settings.clear()
            st.success(f"Stored {n} moderator defaults for {pu}.")
        except Exception as exc:st.error(f"Unable to store defaults: {exc}")
    defaults=shift_rule_defaults(chosen_date,code)
    st.markdown("### Break rules")
    with st.expander("Shift-specific timing, durations and optimization rules",expanded=True):
        st.caption("Timing defaults are derived from the actual imported shift (including 08:00–17:00, 09:00–18:00, 11:00–20:00, and overnight shifts). They may be adjusted for the selected plan.")
        a,b,c,d=st.columns(4)
        with a:
            earliest=st.text_input("Earliest break allowed",defaults["earliest"].strftime("%H:%M"),key=f"early_{code}")
            dur_short=st.number_input("Short length (min)",5,90,15,5,key=f"dshort_{code}")
        with b:
            final=st.text_input("Final break must end by",defaults["final"].strftime("%H:%M"),key=f"final_{code}")
            dur_meal=st.number_input("Meal length (min)",5,90,30,5,key=f"dmeal_{code}")
        with c:
            meal_start=st.text_input("Meal window start",defaults["meal_start"].strftime("%H:%M"),key=f"mstart_{code}")
            dur_wb20=st.number_input("WB20 length (min)",5,90,20,5,key=f"dwb20_{code}")
        with d:
            meal_end=st.text_input("Meal window end",defaults["meal_end"].strftime("%H:%M"),key=f"mend_{code}")
            dur_wb70=st.number_input("WB70 length (min)",5,150,70,5,key=f"dwb70_{code}")
        c1,c2,c3=st.columns(3)
        with c1:min_gap=st.number_input("Minimum inside time (min)",0,180,45,5,key=f"mingap_{code}")
        with c2:max_gap=st.number_input("Maximum inside time (min)",0,240,105,5,key=f"maxgap_{code}")
        with c3:allow_second=st.toggle("Allow WB70 in second half",value=False,key=f"wb_second_{code}",help="When OFF, non-fixed WB70 must finish by the shift midpoint. Fixed WB70 starts always override only this restriction.")
    rule={"earliest":earliest,"final":final,"meal_start":meal_start,"meal_end":meal_end,"min_gap":int(min_gap),"max_gap":int(max_gap),"allow_wb70_second_half":bool(allow_second),"durations":{"Short":int(dur_short),"Meal":int(dur_meal),"WB20":int(dur_wb20),"WB70":int(dur_wb70)}}
    if st.button("Generate optimized break schedule",type="primary"):
        try:
            with st.spinner("Building feasible break sequences and optimizing overlap..."):
                payload=compute_breaks(chosen_date,pu,code,edited.to_dict("records"),rule)
                payload["roster_snapshot_id"]=meta.get("snapshot_id", "")
                st.session_state["generated_payload"]=payload
                st.session_state.pop("result_exports",None)
                st.session_state.pop("individual_png_cache",None)
                st.session_state.pop("individual_zip_cache",None)
            st.success("Schedule ready for review and sharing. No plan was saved to the server.")
        except Exception as exc:
            st.error(f"Unable to generate this schedule: {exc}")
    payload=st.session_state.get("generated_payload")
    if payload:
        if (payload["date"],payload["pu"],payload["shift_code"])!=(chosen_date,pu,code):
            st.info("A generated schedule from a different date/PU/shift is still in this browser session. Select its original date/PU/shift or generate a new one.")
        elif payload.get("roster_snapshot_id")!=meta.get("snapshot_id", ""):
            st.warning("The shared roster changed after this schedule was generated. Regenerate before sharing to reflect the current roster.")
        else:display_generated(payload)


def storage_help():
    st.title("Storage & Setup")
    st.markdown("**Only the shared shift roster and moderator defaults are stored centrally.** Break plans, calendar entries, and previously generated images are never written to Google Sheets.")
    try:
        title=connection_check()
        st.success(f"Connected to private Google Sheet: {title}")
    except Exception as exc:
        st.error(f"Google Sheets not connected: {exc}")
    st.markdown("### Setup")
    st.markdown("1. Open your existing private Google Sheet, which already uses the Google service-account connection from V10.\n2. Keep the same `[break_planner] spreadsheet_id` and `[gcp_service_account]` secrets. V11 creates four new tabs: `Roster`, `RosterVersions`, `RosterControl`, and `ModeratorSettings`. The old `Schedules` and `Breaks` tabs are not used or deleted.\n3. Upload the shift schedule via **Shift Schedule Management**, check flagged duplicates, optionally correct them, and publish.\n4. Team leads then choose Date + PU + Shift on the **Break Planner** page.\n5. Share PNG images or individual cards to moderators through existing chats.")
    st.info("Security: A shared roster includes employee information. Set `access_password` in [break_planner] or use your organization's restricted Streamlit access. Only share the app with authorized WFM/leadership staff.")
    st.code('''[break_planner]\nspreadsheet_id = "YOUR_SHEET_ID"\naccess_password = "A_STRONG_TEAM_ACCESS_PASSWORD"\n\n[gcp_service_account]\n# Keep your existing JSON credential keys from V10 exactly as before.''',language="toml")
    st.markdown("### Permanent cutoff and PU mapping")
    st.write("**Earliest accepted roster date:** 2026-11-04, enforced at upload, storage, editing, and planning.")
    st.dataframe(pd.DataFrame([{"Source worksheet":s,"Power Unit":p} for s,p in PU_SHEETS.items()]),hide_index=True,width="stretch")
    st.markdown("### Recovery")
    st.write("Each roster publication is a separate append-only snapshot. The currently active snapshot is recorded in `RosterControl`; the Shift Schedule Management page can restore an older version. The app never overwrites or stores generated break plans.")


if page=="Shift Schedule Management":schedule_management()
elif page=="Moderator Settings":settings_management()
elif page=="Storage & Help":storage_help()
else:break_planner()
