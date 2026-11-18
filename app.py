"""Lark Break Planner V11 — shared shift-roster driven optimization.
- Upload/edit a common versioned Google Sheets roster; no break-plan calendar or saving.
- Dates earlier than 2026-11-04 are always excluded.
- Fixed 3 Short + 1 Meal + 1 WB20/WB70; optional Ticket coverage.
- Export full-PU imagery, WFM/CSV data, and Lark Base clipboard TSV.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from functools import lru_cache
from io import BytesIO
import base64
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
# The Lark Base TSV/clipboard export is embedded here intentionally.
# It remains functional even when an older lark_base_export.py exists in GitHub.
# No Lark API calls, no person lookup, and no server-side clipboard.
HEADERS = ("Email", "Date", "PU", "Shift", "Break 1", "Break 2",
           "Break 3", "Break 4", "Break 5", "Batch ID", "Status", "Shift Code")
EMOJI_LABELS = {"Short": "☕ Short", "Meal": "🍽️ Meal", "WB20": "💙 Wellness", "WB70": "💙 Wellness"}
MORNING_CODES = frozenset(("0730-1630", "0800-1700", "0900-1800"))
EMAIL_PATTERN = re.compile(r"^[^\s@\t\r\n]+@[^\s@\t\r\n]+\.[^\s@\t\r\n]+$")


class LarkExportError(ValueError):
    """The generated plan is not safe to paste into Lark Base."""


def new_batch_id(selected_date: str, pu: str, shift: str) -> str:
    """Call once when a plan is generated. Keep the result in its payload."""
    date_text = date.fromisoformat(str(selected_date)).strftime("%Y%m%d")
    return f"{date_text}-{pu.upper()}-{shift.upper()}-{uuid.uuid4().hex[:12].upper()}"


def batch_id_from_payload(payload: dict) -> str:
    """Stable upgrade path for plans created before `batch_id` existed."""
    if payload.get("batch_id"):
        return str(payload["batch_id"])
    generated_at = str(payload.get("generated_at") or "")
    if not generated_at:
        raise LarkExportError("Batch ID bulunamadı. Lütfen planı yeniden oluşturun.")
    shift = _classify_shift(payload.get("shift_code", ""))
    identity = "|".join((str(payload.get("date")), str(payload.get("pu")),
                         str(payload.get("shift_code")), generated_at))
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()
    return f"{date.fromisoformat(str(payload['date'])).strftime('%Y%m%d')}-{str(payload['pu']).upper()}-{shift.upper()}-{suffix}"


def _classify_shift(code: str) -> str:
    """Whitelist the planner's actual shift groups; never guess a custom shift."""
    code = str(code or "").strip().upper().replace(" ", "").replace(":", "")
    if code in MORNING_CODES:
        return "Morning"
    if code == "1500-0000":
        return "Mid"
    if code == "2330-0800":
        return "Night"
    raise LarkExportError(
        f"'{code or 'unknown'}' vardiyası Morning, Mid veya Night olarak doğrulanamadı. "
        "Yanlış Lark Base kategorisi oluşturmamak için kopyalama durduruldu."
    )


def _clean_field(value: object, label: str) -> str:
    text = str(value if value is not None else "").strip()
    if any(char in text for char in ("\t", "\r", "\n", "\x00")):
        raise LarkExportError(f"{label} içinde TAB, satır sonu veya geçersiz karakter var.")
    return text


def _break_cell(row: dict, shift_date: date) -> str:
    kind = row["Resource"]
    if kind not in EMOJI_LABELS:
        raise LarkExportError(f"Bilinmeyen mola türü: {kind!r}")
    start, finish = row.get("Start"), row.get("Finish")
    if not isinstance(start, datetime) or not isinstance(finish, datetime) or finish <= start:
        raise LarkExportError("Mola başlangıç veya bitiş zamanı geçersiz.")
    prefix_start = start.strftime("%d/%m ") if start.date() != shift_date else ""
    # If the break itself crosses midnight, display both dates explicitly.
    prefix_end = finish.strftime("%d/%m ") if finish.date() != start.date() else ""
    return f"{EMOJI_LABELS[kind]} {prefix_start}{start:%H:%M}-{prefix_end}{finish:%H:%M}"


def build_lark_base_export(payload: dict) -> tuple[str, list[dict]]:
    """Return pure TSV (without header/trailing newline) and structured preview.

    Validation is fail-closed: no incomplete or ambiguously identified employee
    can be included. Unlike CSV, this uses literal TAB and LF delimiters only.
    """
    schedule = payload.get("schedule")
    if not schedule:
        raise LarkExportError("Oluşturulmuş bir mola planı bulunamadı.")
    try:
        selected_date = date.fromisoformat(str(payload["date"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise LarkExportError("Planın vardiya başlangıç tarihi geçersiz.") from exc
    pu = _clean_field(payload.get("pu"), "PU")
    if pu not in ("HLC", "Agent", "Oncall", "KU", "AZ"):
        raise LarkExportError(f"Geçersiz PU: {pu!r}")
    shift = _classify_shift(payload.get("shift_code"))
    batch_id = _clean_field(batch_id_from_payload(payload), "Batch ID")

    by_name = defaultdict(list)
    for item in schedule:
        if is_excluded_moderator(item):
            raise LarkExportError("Hakan Kaçmaz bu planda bulunuyor. Hariç tutulmuş yeni bir plan oluşturun.")
        name = _clean_field(item.get("Name"), "Moderator adı")
        if not name:
            raise LarkExportError("İsimsiz mola kaydı bulundu.")
        by_name[name].append(item)

    seen_emails = {}
    rows = []
    for name in sorted(by_name, key=str.casefold):
        # Validate before sorting, otherwise a malformed start string mixed
        # with datetimes could raise TypeError and crash the Streamlit page.
        for entry in by_name[name]:
            start, finish = entry.get("Start"), entry.get("Finish")
            if not isinstance(start, datetime) or not isinstance(finish, datetime) or finish <= start:
                raise LarkExportError(f"{name}: mola başlangıç veya bitiş zamanı geçersiz.")
        breaks = sorted(by_name[name], key=lambda b: (b["Start"], b["Finish"]))
        if len(breaks) != 5:
            raise LarkExportError(f"{name}: 5 mola bekleniyor, {len(breaks)} bulundu. Kopyalama durduruldu.")
        emails = {_clean_field(b.get("Email", ""), f"{name} e-postası").casefold() for b in breaks}
        if len(emails) != 1 or not next(iter(emails)):
            raise LarkExportError(f"{name}: e-posta eksik veya molalar arasında farklı e-postalar var.")
        email = next(iter(emails))
        if not EMAIL_PATTERN.fullmatch(email):
            raise LarkExportError(f"{name}: geçersiz roster e-postası '{email}'.")
        if email in seen_emails:
            raise LarkExportError(f"Mükerrer e-posta: {email} ({seen_emails[email]} / {name}).")
        seen_emails[email] = name
        types = Counter(b.get("Resource") for b in breaks)
        if types not in (Counter({"Short": 3, "Meal": 1, "WB20": 1}),
                         Counter({"Short": 3, "Meal": 1, "WB70": 1})):
            raise LarkExportError(f"{name}: beklenen 3 Short + 1 Meal + 1 Wellness dağılımı bozuk ({dict(types)}).")
        seen_intervals = set()
        previous_end = None
        for b in breaks:
            start, end = b.get("Start"), b.get("Finish")
            if not isinstance(start, datetime) or not isinstance(end, datetime) or end <= start:
                raise LarkExportError(f"{name}: geçersiz mola zamanı.")
            if previous_end is not None and start < previous_end:
                raise LarkExportError(f"{name}: çakışan mola kayıtları var.")
            previous_end = end
            if (start, end, b["Resource"]) in seen_intervals:
                raise LarkExportError(f"{name}: mükerrer mola kaydı var.")
            seen_intervals.add((start, end, b["Resource"]))
            if b.get("ShiftStart") and isinstance(b["ShiftStart"], datetime) and b["ShiftStart"].date() != selected_date:
                raise LarkExportError(f"{name}: mola satırındaki vardiya başlangıç tarihi planla uyuşmuyor.")
        # Take Shift Code from this moderator's own roster-backed break rows,
        # never from the selected shift group (a Morning plan can mix codes).
        # Every row must carry the same supported individual code; fail closed
        # rather than silently filling missing or conflicting information.
        actual_codes = []
        for b in breaks:
            raw_code = _clean_field(b.get("ShiftCode"), f"{name} Shift Code")
            if not raw_code:
                raise LarkExportError(f"{name}: gerçek Shift Code eksik. Kopyalama durduruldu.")
            normalized_code = raw_code.upper().replace(" ", "").replace(":", "")
            actual_codes.append(normalized_code)
        if len(set(actual_codes)) != 1:
            raise LarkExportError(
                f"{name}: beş mola kaydında çelişen Shift Code değerleri var "
                f"({', '.join(sorted(set(actual_codes)))}). Kopyalama durduruldu."
            )
        actual_code = actual_codes[0]
        try:
            actual_shift = _classify_shift(actual_code)
        except LarkExportError as exc:
            raise LarkExportError(f"{name}: desteklenmeyen Shift Code '{actual_code}'. Kopyalama durduruldu.") from exc
        if actual_shift != shift:
            raise LarkExportError(
                f"{name}: gerçek Shift Code '{actual_code}' seçili {shift} grubu ile uyuşmuyor."
            )

        cells = [_break_cell(b, selected_date) for b in breaks]
        fields = [email, selected_date.isoformat(), pu, shift, *cells, batch_id, "Draft", actual_code]
        if len(fields) != len(HEADERS):
            raise AssertionError("Lark Base export schema changed unexpectedly.")
        fields = [_clean_field(value, f"{name} sütunu {i+1}") for i, value in enumerate(fields)]
        rows.append(dict(zip(HEADERS, fields)))
    if not rows:
        raise LarkExportError("Exporta dahil edilecek moderatör bulunamadı.")
    tsv = "\n".join("\t".join(row[key] for key in HEADERS) for row in rows)
    # Structural invariant: exactly 12 columns, one record per moderator.
    if len(tsv.splitlines()) != len(rows) or any(len(line.split("\t")) != 12 for line in tsv.splitlines()):
        raise AssertionError("TSV serialization failed 12-column invariant.")
    return tsv, rows


def clipboard_button_html(tsv: str, batch_id: str) -> str:
    """Browser-only Copy button for st.html(..., unsafe_allow_javascript=True).

    No iframe, no server-side clipboard, no file, and no Lark network calls.
    Encode the clipboard payload as base64 so it can never inject markup or JS.
    """
    encoded = base64.b64encode(tsv.encode("utf-8")).decode("ascii")
    safe_id = "lark-copy-" + hashlib.sha256(str(batch_id).encode("utf-8")).hexdigest()[:14]
    button_id = f"{safe_id}-button"
    status_id = f"{safe_id}-status"
    return f"""
<div id="{safe_id}" style="display:flex; flex-wrap:wrap; align-items:center; gap:12px; margin:8px 0 14px;">
  <button id="{button_id}" type="button" style="background:#166534; color:white; border:0; border-radius:9px; padding:12px 20px; font:600 15px system-ui,sans-serif; cursor:pointer;">Copy for Lark Base</button>
  <span id="{status_id}" role="status" aria-live="polite" style="font:14px system-ui,sans-serif;color:#475569;">Yalnızca tarayıcınızın panosuna kopyalanır.</span>
</div>
<script>
(() => {{
  const button = document.getElementById({button_id!r});
  const status = document.getElementById({status_id!r});
  if (!button || !status || button.dataset.larkBound === 'yes') return;
  button.dataset.larkBound = 'yes';
  const bytes = Uint8Array.from(atob({encoded!r}), c => c.charCodeAt(0));
  const tsv = new TextDecoder('utf-8').decode(bytes);
  button.addEventListener('click', async () => {{
    button.disabled = true;
    status.style.color = '#475569';
    status.textContent = 'Kopyalanıyor…';
    let copied = false;
    let failure = '';
    try {{
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API kullanılamıyor');
      await navigator.clipboard.writeText(tsv);
      copied = true;
    }} catch (e) {{
      failure = e?.message || 'Clipboard erişimi reddedildi';
      // Legacy fallback runs on the same user click; verify actual copy result.
      try {{
        const area = document.createElement('textarea');
        area.value = tsv;
        area.setAttribute('readonly', '');
        area.setAttribute('aria-hidden', 'true');
        area.style.position = 'fixed';
        area.style.left = '-9999px';
        document.body.appendChild(area);
        area.focus();
        area.select();
        copied = document.execCommand('copy') === true;
        area.remove();
      }} catch (fallbackError) {{
        failure = fallbackError?.message || failure;
      }}
    }}
    if (copied) {{
      status.style.color = '#166534';
      status.textContent = '✓ Panoya kopyalandı! Lark Base’de ilk boş Email hücresine yapıştırabilirsiniz.';
    }} else {{
      status.style.color = '#b91c1c';
      status.textContent = 'Kopyalama başarısız. Tarayıcının pano iznini kontrol edin: ' + failure;
    }}
    button.disabled = false;
  }});
}})();
</script>"""



st.set_page_config(page_title="Live Break Planner · V11.4.3", page_icon="🗓️",layout="wide")

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
        st.info("Configure the existing private Google Sheets connection in Storage & Help. Generated break schedules are not saved in Google Sheets.")
        return [], {"snapshot_id":"","record_count":0}


def purge_caches():
    cached_roster.clear()
    cached_wb70.clear()


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


def weekly_wb70_management():
    st.title("Weekly WB70 Upload")
    st.caption("Upload the weekly WB70 workbook once for all planners. Names are matched against the published shift roster at planning time. Uploaded assignments are stored in versioned Google Sheets snapshots, not as generated break schedules.")
    try:
        current, meta = cached_wb70()
    except Exception as exc:
        st.error(f"Cannot load shared WB70 assignments: {exc}")
        return
    if current:
        st.success(f"Active WB70 plan: {len(current)} assignments · version {str(meta.get('snapshot_id',''))[:12]}")
    else:
        st.info("No weekly WB70 file has been published yet. Unlisted moderators default to WB20.")
    uploader = st.text_input("Uploaded by (WFM name or initials)", key="wb70_uploader")
    uploaded = st.file_uploader("Upload weekly WB70 Excel workbook (.xlsx)", type=["xlsx"], key="weekly_wb70_file")
    if uploaded is not None:
        try:
            raw = uploaded.getvalue()
            fingerprint = hashlib.sha256(raw).hexdigest()
            entries, coverage, report = parse_wb70_workbook(raw)
            st.dataframe(pd.DataFrame(report), width="stretch", hide_index=True)
            st.caption(f"{len(entries)} WB70 assignments across {len(coverage)} date/PU combinations. Existing assignments outside these dates/PUs are preserved. Empty days in uploaded sheets clear earlier WB70 assignments for that day and PU.")
            st.dataframe(pd.DataFrame(entries), width="stretch", hide_index=True)
            roster, _ = load_remote_or_error()
            unmatched = []
            valid_roster = {(r['date'], r['pu'], normalize_name(r['name'])): r for r in roster}
            for e in entries:
                found = valid_roster.get((e['date'], e['pu'], normalize_name(e['name'])))
                if not found:
                    unmatched.append(e)
                else:
                    code = canonical_shift(found.get('shift_code'))
                    actual_shift = ('Morning' if code in MORNING_SHIFT_CODES else
                                    'Mid' if code == '1500-0000' else
                                    'Night' if code == '2330-0800' else 'Custom')
                    if actual_shift != e['shift']:
                        unmatched.append(e)
            if unmatched:
                st.warning(f"{len(unmatched)} entries do not currently match a moderator and shift in the shared roster. They are still imported, but will not auto-apply unless the roster is corrected.")
                st.dataframe(pd.DataFrame(unmatched), width="stretch", hide_index=True)
            if fingerprint == st.session_state.get('wb70_last_published'):
                st.info('This exact file has already been published during this browser session.')
            elif st.button('Publish weekly WB70 assignments', type='primary'):
                outcome = publish_wb70_plan(entries, coverage, uploader, expected_snapshot=str(meta.get('snapshot_id','')))
                st.session_state['wb70_last_published'] = fingerprint
                purge_caches()
                st.success(f"Published {outcome['record_count']} current WB70 assignments.")
                st.rerun()
        except Exception as exc:
            st.error(f"WB70 upload error: {exc}")
    with st.expander("WB70 publication history / restore", expanded=False):
        try:
            revisions = list_wb70_versions()
            if revisions:
                st.dataframe(pd.DataFrame(revisions).tail(20), width="stretch", hide_index=True)
                ids = [str(v['snapshot_id']) for v in reversed(revisions)]
                chosen = st.selectbox('Published WB70 version', ids, key='wb70_restore')
                if st.button('Restore WB70 version'):
                    if not uploader.strip():
                        st.error('Enter uploader name before restoring.')
                    else:
                        restore_wb70_version(chosen, uploader)
                        purge_caches()
                        st.rerun()
        except Exception as exc:
            st.error(f'Cannot read WB70 revision history: {exc}')


# Morning staff on these alternate schedules are optimized together. Individual
# shift boundaries and exported WFM working intervals are always preserved.
MORNING_SHIFT_CODES = ("0730-1630", "0800-1700", "0900-1800")


def selected_shift_codes(code):
    return set(MORNING_SHIFT_CODES) if canonical_shift(code) == "0730-1630" else {canonical_shift(code)}


def morning_group_label(codes):
    if len(codes) <= 1:
        return shift_label(codes[0]) if codes else "Morning"
    return "Morning (" + ", ".join(
        f"{parse_shift_code(c)[0]}–{parse_shift_code(c)[1]}" for c in codes
    ) + ")"


def shift_rule_defaults(selected_date, code, pu=None):
    times = parse_shift_code(code)
    if not times:
        raise ValueError(f"Invalid shift: {code}")
    sh, eh = times
    start = datetime.combine(date.fromisoformat(selected_date), time.fromisoformat(sh))
    end = datetime.combine(date.fromisoformat(selected_date), time.fromisoformat(eh))
    if end <= start:
        end += timedelta(days=1)
    canonical = canonical_shift(code)
    preset = (
        "Morning" if canonical in MORNING_SHIFT_CODES else
        "Mid" if canonical == "1500-0000" else
        "Night" if canonical == "2330-0800" else "Custom"
    )
    pressure_class = preset
    if preset == "Custom" and 5 <= start.hour < 12 and end.hour <= 20:
        pressure_class = "Morning"
    if preset == "Custom" and 14 <= start.hour < 20:
        pressure_class = "Mid"
    # HLC default inside and boundary margins are 45–105; all other main
    # PU shifts retain 60–90. All rules remain editable at plan time.
    hlc = pu == "HLC" and preset in ("Morning", "Mid", "Night")
    start_gap = 45 if hlc else 60
    end_gap = 45 if hlc else (60 if preset != "Custom" else 45)
    earliest = start + timedelta(minutes=start_gap)
    final = end - timedelta(minutes=end_gap)
    if preset == "Morning":
        meal_start = start.replace(hour=12, minute=0)
        meal_end = start.replace(hour=14, minute=30)
    elif preset == "Mid":
        meal_start = start.replace(hour=17, minute=0)
        meal_end = start.replace(hour=21, minute=0)
    elif preset == "Night":
        meal_start = start + timedelta(hours=2, minutes=30)
        meal_end = start + timedelta(hours=5, minutes=30)
    elif 5 <= start.hour <= 10:
        meal_start = start.replace(hour=12, minute=0)
        meal_end = start.replace(hour=14, minute=30)
    else:
        meal_start = start + timedelta(hours=3)
        meal_end = start + timedelta(hours=6)
    return {
        "start": start, "end": end, "earliest": earliest, "final": final,
        "meal_start": meal_start, "meal_end": meal_end, "preset": preset,
        "pressure_class": pressure_class,
    }

def resolve_clock(clock,shift_start):
    try:
        hh,mm=map(int,str(clock).strip().split(":"))
        if not 0<=hh<=23 or not 0<=mm<=59:raise ValueError
        dt=shift_start.replace(hour=hh,minute=mm)
        if dt<shift_start:dt+=timedelta(days=1)
        return dt
    except Exception as exc:
        raise ValueError(f"Time '{clock}' is invalid; please use HH:MM.") from exc


def compute_breaks(selected_date, pu, code, chosen_rows, rule):
    """Optimize actual (potentially mixed) shift hours with shared PU coverage.

    Morning (07:30–16:30, 08:00–17:00, 09:00–18:00) runs one global
    optimization on a shared wall-clock axis. Each person keeps their own
    9-hour shift, first/last-break bounds and complete WFM shift timeline.
    """
    defaults = shift_rule_defaults(selected_date, code, pu)
    anchor_start = defaults["start"]
    anchor_end = defaults["end"]
    common_earliest = resolve_clock(rule["earliest"], anchor_start)
    common_final = resolve_clock(rule["final"], anchor_start)
    meal_start = resolve_clock(rule["meal_start"], anchor_start)
    meal_end = resolve_clock(rule["meal_end"], anchor_start)
    if meal_end <= meal_start:
        raise ValueError("Meal window end must follow its start.")
    if not (anchor_start < common_earliest < common_final < anchor_end):
        raise ValueError("Break-window boundaries must lie strictly inside the shift in chronological order.")
    min_gap, max_gap = int(rule["min_gap"]), int(rule["max_gap"])
    if not (0 <= min_gap <= max_gap):
        raise ValueError("Minimum inside time cannot exceed maximum inside time.")

    chosen = [row for row in chosen_rows
              if bool(row.get("Include", False)) and not is_excluded_moderator(row)]
    if not chosen:
        raise ValueError("No moderators are included in the plan.")
    allowed = selected_shift_codes(code)
    selected_is_morning = canonical_shift(code) == "0730-1630"
    mod_shift_info = []
    for row in chosen:
        actual_code = canonical_shift(row.get("ShiftCode") or code)
        if actual_code not in allowed:
            raise ValueError(f"{row['Name']}: shift {actual_code} is not included in the selected shift group.")
        actual_defaults = shift_rule_defaults(selected_date, actual_code, pu)
        person_start, person_end = actual_defaults["start"], actual_defaults["end"]
        if person_end - person_start > timedelta(hours=13):
            raise ValueError(f"{row['Name']}: shifts longer than 13 hours are not supported.")
        if int((person_end - person_start).total_seconds() / 60) % TIME_STEP:
            raise ValueError(f"{row['Name']}: shift must use five-minute boundaries.")
        # For alternate Morning starts, shift the *earliest* and *latest* time
        # controls relative to the 07:30–16:30 base shift. This keeps the
        # selected Morning rules customizable while maintaining 60–90 minute
        # inside constraints for a 09:00–18:00 moderator.
        delta = (person_start - anchor_start) if selected_is_morning else timedelta(0)
        person_earliest = common_earliest + delta
        person_final = common_final + delta
        if not (person_start < person_earliest < person_final < person_end):
            raise ValueError(f"{row['Name']}: custom break window falls outside their actual {actual_code} shift.")
        mod_shift_info.append({
            "row": row, "code": actual_code, "start": person_start, "end": person_end,
            "earliest": person_earliest, "final": person_final,
        })

    start = min(m["start"] for m in mod_shift_info)
    end = max(m["end"] for m in mod_shift_info)
    total = int((end - start).total_seconds() / 60)
    if total % TIME_STEP:
        raise ValueError("Combined shift timeline must use five-minute boundaries.")

    candidate_cap = min(300, max(90, 3600 // len(chosen)))
    moderators = []
    profile_cache = {}
    for item in mod_shift_info:
        row = item["row"]
        name = str(row["Name"])
        wb70 = bool(row["WB70"])
        counts = {"Short": 3, "Meal": 1, "WB20": 0 if wb70 else 1, "WB70": 1 if wb70 else 0}
        dur = dict(rule["durations"])
        custom_duration = str(row.get("WB70 Duration (mins)", "") or "").strip()
        if wb70 and custom_duration:
            if not custom_duration.isdigit() or int(custom_duration) % TIME_STEP or int(custom_duration) < TIME_STEP:
                raise ValueError(f"{name}: WB70 duration must be a positive multiple of 5 minutes.")
            dur["WB70"] = int(custom_duration)
        fixed_clock = str(row.get("Fixed WB70 Start", "") or "").strip()
        if fixed_clock and not wb70:
            raise ValueError(f"{name}: Fixed WB70 requires WB70 checkbox to be checked.")
        fixed_mins = None
        if fixed_clock:
            fixed_dt = resolve_clock(fixed_clock, item["start"])
            fixed_mins = int((fixed_dt - item["start"]).total_seconds() / 60)
            if fixed_mins % TIME_STEP:
                raise ValueError(f"{name}: Fixed WB70 must be on a five-minute boundary.")
            # A fixed WB70 is non-negotiable. The shift itself, however, must
            # contain the WB70 and its full, unchanged duration.
            shift_minutes = int((item["end"] - item["start"]).total_seconds() / 60)
            if fixed_mins < 0 or fixed_mins + dur["WB70"] > shift_minutes:
                raise ValueError(
                    f"{name}: the uploaded fixed WB70 {fixed_clock} ({dur['WB70']} min) "
                    f"falls outside the actual shift {item['code']}. "
                    "This WB70 cannot be moved or shortened automatically."
                )
        person_start = item["start"]
        person_end = item["end"]
        # HLC final Morning break must fit inside overlap; Mid first break
        # must START in overlap (it may end after 16:30, e.g. fixed WB70).
        overlap_day = person_start.date()
        overlap_start = datetime.combine(overlap_day, time(15, 0))
        overlap_end = datetime.combine(overlap_day, time(16, 30))
        first_overlap = (
            (int((overlap_start-person_start).total_seconds()/60),
             int((overlap_end-person_start).total_seconds()/60))
            if pu == "HLC" and defaults["preset"] == "Mid" else None
        )
        last_overlap = (
            (int((overlap_start-person_start).total_seconds()/60),
             int((overlap_end-person_start).total_seconds()/60))
            if pu == "HLC" and defaults["preset"] == "Morning" else None
        )
        profile = (
            tuple((b, counts[b]) for b in BREAK_TYPES),
            tuple((b, dur[b]) for b in BREAK_TYPES),
            int((person_end - person_start).total_seconds() / 60),
            int((item["earliest"] - person_start).total_seconds() / 60),
            int((item["final"] - person_start).total_seconds() / 60),
            int((meal_start - person_start).total_seconds() / 60),
            int((meal_end - person_start).total_seconds() / 60),
            fixed_mins, bool(rule["allow_wb70_second_half"]),
            first_overlap, last_overlap, min_gap, max_gap,
        )
        if profile not in profile_cache:
            base_args = dict(
                counts=counts, durations=dur,
                total_shift_mins=profile[2], earliest_mins=profile[3],
                final_mins=profile[4], meal_start_mins=profile[5],
                meal_end_mins=profile[6], min_inside=min_gap, max_inside=max_gap,
                fixed_wb70_mins=fixed_mins,
                allow_wb70_second_half=bool(rule["allow_wb70_second_half"]),
                max_patterns=candidate_cap,
                first_break_window=first_overlap,
                last_break_window=last_overlap,
            )
            # An uploaded fixed WB70 ALWAYS wins. Prefer layouts that obey
            # normal rules; if that is impossible, relax restrictions around
            # the WB70, and finally release secondary restrictions altogether.
            patterns = []
            applied_mode = "strict"
            modes = ("strict", "neighbor", "all") if fixed_mins is not None else ("strict",)
            for mode in modes:
                patterns = build_candidate_patterns(**base_args, wb70_priority_mode=mode)
                if patterns:
                    applied_mode = mode
                    break
            if not patterns:
                detail = (
                    f"Fixed WB70 {fixed_clock} could not coexist with the other four nonoverlapping breaks inside their actual shift."
                    if fixed_mins is not None else
                    "Check the break window, inside time limits, meal window, and no-consecutive-Long rule."
                )
                raise ValueError(f"{name} ({item['code']}): no feasible five-break layout. {detail}")
            profile_cache[profile] = (patterns, applied_mode)
        applied_mode = profile_cache[profile][1]
        moderators.append({
            "Name": name, "Email": str(row.get("Email", "") or "").strip().lower(),
            "Counts": counts, "Durations": dur,
            "TicketModerator": bool(row["Ticket Moderator"]),
            "Profile": profile, "ShiftCode": item["code"],
            "ShiftStart": person_start, "ShiftEnd": person_end,
            "Earliest": item["earliest"], "Final": item["final"],
            "WB70PriorityMode": applied_mode,
            "FixedWB70Start": (person_start + timedelta(minutes=fixed_mins)) if fixed_mins is not None else None,
            "FixedWB70Duration": dur["WB70"] if fixed_mins is not None else 0,
        })

    tickets = [m for m in moderators if m["TicketModerator"]]
    if len(tickets) == 1:
        raise ValueError(
            f"Only one Ticket Moderator is selected ({tickets[0]['Name']}). "
            "Mark a second Ticket Moderator or uncheck the first."
        )

    timeline = list(range(0, total + 1, TIME_STEP))
    weights = build_pressure_profile(defaults["pressure_class"], start, timeline)["Weight"]
    pattern_sets, vector_sets = [], []
    vector_cache = {}
    for mod in moderators:
        patterns = profile_cache[mod["Profile"]][0]
        pattern_sets.append(patterns)
        offset = int((mod["ShiftStart"] - start).total_seconds() / 60)
        vector_key = (mod["Profile"], offset)
        if vector_key not in vector_cache:
            cols = []
            for pattern in patterns:
                absolute_pattern = {
                    "Order": pattern["Order"],
                    "Starts": tuple(int(m) + offset for m in pattern["Starts"]),
                }
                cols.append(pattern_vectors(absolute_pattern, mod["Durations"], timeline))
            vector_cache[vector_key] = (
                np.stack([a for a, w in cols], axis=1),
                np.stack([w for a, w in cols], axis=1),
            )
        vector_sets.append(vector_cache[vector_key])

    ticket_availability = []
    ticket_forced_override = []
    for minute in timeline:
        now = start + timedelta(minutes=minute)
        active_tickets = [m for m in tickets if m["ShiftStart"] <= now < m["ShiftEnd"]]
        forced = [m for m in active_tickets if (
            m["FixedWB70Start"] is not None
            and m["FixedWB70Start"] <= now < m["FixedWB70Start"] + timedelta(minutes=m["FixedWB70Duration"])
        )]
        # Only when *every* on-duty ticket moderator is on an immovable
        # fixed WB70 do we permit the unavoidable ticket coverage exception.
        override = bool(active_tickets and len(forced) == len(active_tickets))
        ticket_forced_override.append(override)
        ticket_availability.append(len(active_tickets) + int(override))
    result = optimize_pattern_selection(
        moderators, pattern_sets, vector_sets, timeline, weights,
        ticket_available_counts=ticket_availability,
    )
    schedule = []
    for i, mod in enumerate(moderators):
        pattern = pattern_sets[i][result["Chosen"][i]]
        for kind, minutes in zip(pattern["Order"], pattern["Starts"]):
            t = mod["ShiftStart"] + timedelta(minutes=int(minutes))
            finish = t + timedelta(minutes=mod["Durations"][kind])
            schedule.append({
                "Name": mod["Name"], "Email": mod["Email"], "Resource": kind,
                "Start": t, "Finish": finish, "TicketModerator": mod["TicketModerator"],
                "ShiftCode": mod["ShiftCode"], "ShiftStart": mod["ShiftStart"],
                "ShiftEnd": mod["ShiftEnd"], "ShiftLabel": shift_label(mod["ShiftCode"]),
            })

    # Audit every individual schedule against its actual working hours.
    for mod in moderators:
        seq = sorted([b for b in schedule if b["Name"] == mod["Name"]], key=lambda b: b["Start"])
        if len(seq) != 5 or Counter(b["Resource"] for b in seq) != {k: v for k, v in mod["Counts"].items() if v}:
            raise RuntimeError(f"Entitlement-validation error for {mod['Name']}")
        if seq[0]["Resource"] == "Meal":
            raise RuntimeError(f"Meal-first violation for {mod['Name']}")
        if (seq[0]["Start"] < mod["ShiftStart"] or seq[-1]["Finish"] > mod["ShiftEnd"]
                or any(a["Finish"] > b["Start"] for a, b in zip(seq, seq[1:]))):
            raise RuntimeError(f"Invalid break overlap or shift boundary for {mod['Name']}")
        if mod["FixedWB70Start"] is not None:
            fixed_b = next(b for b in seq if b["Resource"] == "WB70")
            if fixed_b["Start"] != mod["FixedWB70Start"] or (
                fixed_b["Finish"]-fixed_b["Start"]
            ) != timedelta(minutes=mod["FixedWB70Duration"]):
                raise RuntimeError(f"Fixed WB70 priority validation failed for {mod['Name']}")

        gap_pairs = (
            [(mod["ShiftStart"], seq[0]["Start"]), (seq[-1]["Finish"], mod["ShiftEnd"])]
            + [(a["Finish"], b["Start"]) for a, b in zip(seq, seq[1:])]
        )
        work_gaps = [(b-a).total_seconds()/60 for a,b in gap_pairs]
        chronological_gaps = [work_gaps[0]] + work_gaps[2:] + [work_gaps[1]]
        # Only fixed WB70 moderators whose usual rules proved infeasible may
        # bypass inside/Long/overlap/window checks; all others are hard-checked.
        if mod["WB70PriorityMode"] == "strict":
            if any(g < min_gap - 0.01 or g > max_gap + 0.01 for g in work_gaps):
                raise RuntimeError(f"Inside-gap validation failure for {mod['Name']}")
            if any(a >= 90-0.01 and b >= 90-0.01 for a,b in zip(chronological_gaps, chronological_gaps[1:])):
                raise RuntimeError(f"Consecutive Long inside times for {mod['Name']}")
            if pu == 'HLC' and defaults['preset'] == 'Morning':
                if seq[-1]['Start'] < datetime.combine(mod['ShiftStart'].date(), time(15,0)) or seq[-1]['Finish'] > datetime.combine(mod['ShiftStart'].date(), time(16,30)):
                    raise RuntimeError(f"Morning overlap violation for {mod['Name']}")
            if pu == 'HLC' and defaults['preset'] == 'Mid':
                if not datetime.combine(mod['ShiftStart'].date(), time(15,0)) <= seq[0]['Start'] <= datetime.combine(mod['ShiftStart'].date(), time(16,30)):
                    raise RuntimeError(f"Mid overlap violation for {mod['Name']}")
            if seq[0]["Start"] < mod["Earliest"] or seq[-1]["Finish"] > mod["Final"]:
                raise RuntimeError(f"Break-window validation failure for {mod['Name']}")

    if len(tickets) >= 2:
        ticket_names = {m["Name"] for m in tickets}
        for t_idx, minute in enumerate(timeline):
            now = start + timedelta(minutes=minute)
            available = sum(m["ShiftStart"] <= now < m["ShiftEnd"] for m in tickets)
            on_break = sum(b["Name"] in ticket_names and b["Start"] <= now < b["Finish"] for b in schedule)
            if available and on_break >= available and not ticket_forced_override[t_idx]:
                raise RuntimeError("Ticket Moderator coverage validation failed")

    all_codes = sorted({m["ShiftCode"] for m in moderators})
    label = morning_group_label(all_codes) if selected_is_morning else shift_label(code)
    return {
        "date": selected_date, "pu": pu, "shift_code": canonical_shift(code),
        "shift_label": label,
        "shift_start_dt": start, "shift_end_dt": end,
        "earliest_dt": min(m["Earliest"] for m in moderators),
        "final_dt": max(m["Final"] for m in moderators),
        "schedule": schedule,
        "peak_concurrent": max(
            sum(b["Start"] <= start + timedelta(minutes=t) < b["Finish"] for b in schedule)
            for t in timeline
        ),
        "peak_wb70": max(
            sum(b["Resource"] == "WB70" and b["Start"] <= start + timedelta(minutes=t) < b["Finish"] for b in schedule)
            for t in timeline
        ),
        "tickets": len(tickets), "used_fallback": bool(result["UsedFallback"]),
        "allow_wb70_second_half": bool(rule["allow_wb70_second_half"]),
        "wb70_priority_overrides": [m["Name"] for m in moderators if m["WB70PriorityMode"] != "strict"],
        "wb70_ticket_coverage_exception": any(ticket_forced_override),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pressure_summary": pd.DataFrame({
            "Time": [start + timedelta(minutes=t) for t in timeline],
            "Pressure Weight": weights,
        }),
    }

def display_generated(payload):
    if any(is_excluded_moderator(entry) for entry in payload.get("schedule", [])):
        st.error("Bu plan kalıcı olarak hariç tutulan bir moderatörü içeriyor. Devam etmeden planı yeniden oluşturun.")
        return
    st.markdown("### Generated break schedule")
    info=st.columns(4)
    with info[0]:st.metric("Moderators",len({b["Name"] for b in payload["schedule"]}))
    with info[1]:st.metric("Peak concurrent breaks",payload["peak_concurrent"])
    with info[2]:st.metric("Peak simultaneous WB70s",payload["peak_wb70"])
    with info[3]:st.metric("Ticket moderators",payload["tickets"])
    if payload["used_fallback"]:st.warning("Global optimizer did not converge to a MILP solution in time, so the fully feasible ticket-safe fallback was used.")
    st.caption("All five breaks are validated against the shift. Fixed uploaded WB70 times take precedence if standard rules conflict. All other moderators follow the normal inside, overlap and consecutive-Long restrictions. Generated plans remain in this browser session only.")
    if payload.get("wb70_priority_overrides"):
        st.warning("Fixed WB70 priority required relaxing secondary scheduling rules for: " + ", ".join(payload["wb70_priority_overrides"]))
    if payload.get("wb70_ticket_coverage_exception"):
        st.warning("Fixed WB70 assignments create unavoidable periods without Ticket Moderator coverage. Review these assignments before use.")
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
    st.markdown("#### WFM planned shift record")
    st.caption("Full planned shift timeline in WFM's CSV layout: Date, Email, Status, Hours, Start_time, and End_time. Other source-system columns are blank.")
    try:
        wfm_csv = generate_wfm_shift_record_csv(payload)
    except ValueError as exc:
        st.warning(f"WFM export unavailable: {exc}")
    else:
        st.download_button(
            "Download WFM planned shift record (CSV)",
            data=wfm_csv,
            file_name=name_for("WFM_Planned_Shift_Record", payload, "csv"),
            mime="text/csv",
        )
    st.markdown("#### Lark Base Export")
    try:
        lark_tsv, lark_rows = build_lark_base_export(payload)
    except LarkExportError as exc:
        st.warning(f"Lark Base kopyalama kullanılamıyor: {exc}")
    else:
        st.caption(f"{len(lark_rows)} moderatör · 12 sütun · başlıksız TAB-ayrılmış veri · Status: Draft")
        with st.expander("Lark Base önizlemesi (ilk 3 moderatör)", expanded=False):
            st.table(pd.DataFrame([{
                "Email": r["Email"], "Date": r["Date"], "PU": r["PU"],
                "Shift": r["Shift"], "Break 1": r["Break 1"],
                "Batch ID": r["Batch ID"], "Status": r["Status"],
                "Shift Code": r["Shift Code"]
            } for r in lark_rows[:3]]))
        st.html(
            clipboard_button_html(lark_tsv, lark_rows[0]["Batch ID"]),
            unsafe_allow_javascript=True,
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
    pu_options=[pu for pu in PUS if any(r["date"]==chosen_date and r["pu"]==pu and not is_excluded_moderator(r) for r in roster)]
    with cols[1]:pu=st.selectbox("Power Unit",pu_options,key="planner_pu") if pu_options else None
    if not pu:
        st.info("No Power Units available for the selected date.")
        return
    raw_shift_counts=Counter(canonical_shift(r["shift_code"]) for r in roster if r["date"]==chosen_date and r["pu"]==pu and parse_shift_code(r["shift_code"]) and not is_excluded_moderator(r))
    shift_counts=Counter(raw_shift_counts)
    morning_total=sum(raw_shift_counts[c] for c in MORNING_SHIFT_CODES)
    if morning_total:
        for c in MORNING_SHIFT_CODES:
            shift_counts.pop(c, None)
        shift_counts["0730-1630"]=morning_total
    shifts=sorted([c for c in shift_counts if c],key=lambda c:c[:4])
    def shift_option_label(c):
        if c=="0730-1630":
            return f"Morning · includes 07:30, 08:00 & 09:00 starts ({shift_counts[c]} moderators)"
        return f"{shift_label(c)} ({shift_counts[c]} moderators)"
    with cols[2]:code=st.selectbox("Scheduled shift",shifts,format_func=shift_option_label,key="planner_shift") if shifts else None
    if not code:
        st.warning("No timed shifts found for this selected date and Power Unit. Check the roster status codes.")
        return
    applicable_codes = selected_shift_codes(code)
    conflicting=[]
    conflict_keys=set()
    for selected_code in applicable_codes:
        for issue in affected_conflicts(roster,chosen_date,pu,selected_code):
            if is_excluded_moderator({"Name": issue.get("Moderator", "")}):
                continue
            key=(issue.get("Date"), issue.get("Moderator"), issue.get("Issue"))
            if key not in conflict_keys:
                conflict_keys.add(key)
                conflicting.append(issue)
    if conflicting:
        st.error("ROSTER CONFLICT — This shift contains an active moderator also assigned to another PU or twice in the same PU on this date. Fix the highlighted assignments in Shift Schedule Management before generating.")
        st.dataframe(pd.DataFrame(conflicting),width="stretch",hide_index=True)
        return
    rows=[r for r in roster if r["date"]==chosen_date and r["pu"]==pu and canonical_shift(r["shift_code"]) in applicable_codes and not is_excluded_moderator(r)]
    try:
        weekly_wb70, wb70_meta = cached_wb70()
    except Exception as exc:
        st.error(f"Cannot load the published weekly WB70 plan: {exc}")
        st.info("Refresh or check the shared Google Sheets connection before generating a plan. WB70 assignments must not be silently omitted.")
        return
    choices = []
    for r in sorted(rows, key=lambda x: x["name"].casefold()):
        shift_code = canonical_shift(r['shift_code'])
        section = ('Morning' if shift_code in MORNING_SHIFT_CODES else
                   'Mid' if shift_code == '1500-0000' else
                   'Night' if shift_code == '2330-0800' else 'Custom')
        assigned = match_wb70(weekly_wb70, chosen_date, pu, section, r['name'])
        choices.append({
            "Name":r["name"], "ShiftCode":shift_code, "Shift":shift_label(r["shift_code"]),
            "Include":True, "WB70":assigned is not None, "Ticket Moderator":False,
            "Fixed WB70 Start":str(assigned.get('fixed_start', '') or '') if assigned else '',
            "WB70 Duration (mins)":str(assigned.get('duration', '') or '') if assigned else '',
            "Identity":person_identity(r),
        })
    st.markdown(f"### {len(choices)} moderators · {pu} · {shift_option_label(code)}")
    if code=="0730-1630":
        details=", ".join(f"{shift_label(c)}: {raw_shift_counts[c]}" for c in MORNING_SHIFT_CODES if raw_shift_counts[c])
        st.caption(f"Morning group shift composition — {details}. All staff are optimized together, but each retains their actual shift hours.")
    st.caption("WB70 assignments and fixed times come from the published weekly workbook. A fixed WB70 takes priority if normal constraints conflict; other schedules retain the no-consecutive-Long rule. Per-plan overrides remain temporary.")
    edited=st.data_editor(pd.DataFrame(choices),width="stretch",hide_index=True,disabled=["Name","Identity","ShiftCode","Shift"],num_rows="fixed",column_config={"Identity":None,"ShiftCode":None,"Include":st.column_config.CheckboxColumn("Include"),"WB70":st.column_config.CheckboxColumn("WB70?"),"Ticket Moderator":st.column_config.CheckboxColumn("Ticket?"),"Fixed WB70 Start":st.column_config.TextColumn("Fixed WB70 HH:MM"),"WB70 Duration (mins)":st.column_config.TextColumn("WB70 duration (optional)")},key=f"plan_edit_v4_{chosen_date}_{pu}_{code}_{meta.get('snapshot_id','')}_{wb70_meta.get('snapshot_id','')}")
    defaults=shift_rule_defaults(chosen_date,code,pu)
    st.markdown("### Break rules")
    with st.expander("Shift-specific timing, durations and optimization rules",expanded=True):
        st.caption("Rules remain fully customizable. HLC uses 45–105 minute inside limits; other PUs retain 60–90 minutes. Fixed WB70 takes priority over conflicting timing and Long rules; all other breaks follow the selected rules.")
        a,b,c,d=st.columns(4)
        with a:
            earliest=st.text_input("Earliest break allowed",defaults["earliest"].strftime("%H:%M"),key=f"early_rules_v4_{pu}_{code}")
            dur_short=st.number_input("Short length (min)",5,90,15,5,key=f"dshort_{code}")
        with b:
            final=st.text_input("Final break must end by",defaults["final"].strftime("%H:%M"),key=f"final_rules_v4_{pu}_{code}")
            dur_meal=st.number_input("Meal length (min)",5,90,30,5,key=f"dmeal_{code}")
        with c:
            meal_start=st.text_input("Meal window start",defaults["meal_start"].strftime("%H:%M"),key=f"mstart_{code}")
            dur_wb20=st.number_input("WB20 length (min)",5,90,20,5,key=f"dwb20_{code}")
        with d:
            meal_end=st.text_input("Meal window end",defaults["meal_end"].strftime("%H:%M"),key=f"mend_{code}")
            dur_wb70=st.number_input("WB70 length (min)",5,150,70,5,key=f"dwb70_{code}")
        c1,c2,c3=st.columns(3)
        with c1:min_gap=st.number_input("Minimum inside time (min)",0,180,(45 if pu == "HLC" and defaults["preset"] != "Custom" else 60 if defaults["preset"] != "Custom" else 45),5,key=f"mingap_rules_v4_{pu}_{code}")
        with c2:max_gap=st.number_input("Maximum inside time (min)",0,240,(105 if pu == "HLC" and defaults["preset"] != "Custom" else 90 if defaults["preset"] != "Custom" else 105),5,key=f"maxgap_rules_v4_{pu}_{code}")
        with c3:allow_second=st.toggle(
            "Allow WB70 in second half",
            value=defaults["preset"] in ("Morning", "Night"),
            key=f"wb_second_v1141_{pu}_{code}",
            help="Default: Morning ON, Mid OFF, Night ON. All PUs. Customizable; fixed uploaded WB70 starts always take priority."
        )
    rule={"earliest":earliest,"final":final,"meal_start":meal_start,"meal_end":meal_end,"min_gap":int(min_gap),"max_gap":int(max_gap),"allow_wb70_second_half":bool(allow_second),"durations":{"Short":int(dur_short),"Meal":int(dur_meal),"WB20":int(dur_wb20),"WB70":int(dur_wb70)}}
    if st.button("Generate optimized break schedule",type="primary"):
        try:
            with st.spinner("Building feasible break sequences and optimizing overlap..."):
                # Attach verified emails from the published roster, not editable name text.
                email_by_identity = {person_identity(r): str(r.get("email", "") or "").strip().lower() for r in rows}
                selection = edited.to_dict("records")
                for selected in selection:
                    selected["Email"] = email_by_identity.get(selected["Identity"], "")
                payload=compute_breaks(chosen_date,pu,code,selection,rule)
                try:
                    batch_shift = _classify_shift(code)
                except LarkExportError:
                    # Custom shifts are still schedulable, but never assigned a
                    # guessed Morning/Mid/Night Lark category.
                    batch_shift = canonical_shift(code)
                payload["batch_id"] = new_batch_id(chosen_date, pu, batch_shift)
                payload["roster_snapshot_id"]=meta.get("snapshot_id", "")
                payload["wb70_snapshot_id"]=wb70_meta.get("snapshot_id", "")
                st.session_state["generated_payload"]=payload
                st.session_state.pop("result_exports",None)
            st.success("Schedule ready for review and sharing. No plan was saved to the server.")
        except Exception as exc:
            st.error(f"Unable to generate this schedule: {exc}")
    payload=st.session_state.get("generated_payload")
    if payload:
        if (payload["date"],payload["pu"],payload["shift_code"])!=(chosen_date,pu,code):
            st.info("A generated schedule from a different date/PU/shift is still in this browser session. Select its original date/PU/shift or generate a new one.")
        elif payload.get("roster_snapshot_id")!=meta.get("snapshot_id", ""):
            st.warning("The shared roster changed after this schedule was generated. Regenerate before sharing to reflect the current roster.")
        elif payload.get("wb70_snapshot_id")!=wb70_meta.get("snapshot_id", ""):
            st.warning("The published weekly WB70 plan changed. Regenerate before sharing to reflect the current assignments.")
        else:display_generated(payload)


def storage_help():
    st.title("Storage & Setup")
    st.markdown("**Only the shared shift roster and published weekly WB70 assignments are stored centrally.** Ticket/WB70 per-plan overrides, break plans and images are never stored.")
    try:
        title=connection_check()
        st.success(f"Connected to private Google Sheet: {title}")
    except Exception as exc:
        st.error(f"Google Sheets not connected: {exc}")
    st.markdown("### Setup")
    st.markdown("1. Open your existing private Google Sheet, which already uses the Google service-account connection from V10.\n2. Keep the same `[break_planner] spreadsheet_id` and `[gcp_service_account]` secrets. The app uses roster tabs and three new WB70 tabs: `WB70Plan`, `WB70Versions`, and `WB70Control`. The old `Schedules` and `Breaks` tabs are not used or deleted.\n3. Publish the shift roster through **Shift Schedule Management**.\n4. Publish the WB70 workbook through **Weekly WB70 Upload**.\n5. Choose Date + PU + Shift, review the plan, and use **Copy for Lark Base**. Full-PU PNG and WFM CSV downloads remain available.")
    st.info("Security: A shared roster includes employee information. Set `access_password` in [break_planner] or use your organization's restricted Streamlit access. Only share the app with authorized WFM/leadership staff.")
    st.code('''[break_planner]\nspreadsheet_id = "YOUR_SHEET_ID"\naccess_password = "A_STRONG_TEAM_ACCESS_PASSWORD"\n\n[gcp_service_account]\n# Keep your existing JSON credential keys from V10 exactly as before.''',language="toml")
    st.markdown("### Permanent cutoff and PU mapping")
    st.write("**Earliest accepted roster date:** 2026-11-04, enforced at upload, storage, editing, and planning.")
    st.dataframe(pd.DataFrame([{"Source worksheet":s,"Power Unit":p} for s,p in PU_SHEETS.items()]),hide_index=True,width="stretch")
    st.markdown("### Recovery")
    st.write("Each roster publication is a separate append-only snapshot. The currently active snapshot is recorded in `RosterControl`; the Shift Schedule Management page can restore an older version. The app never overwrites or stores generated break plans.")


if page=="Shift Schedule Management":schedule_management()
elif page=="Weekly WB70 Upload":weekly_wb70_management()
elif page=="Storage & Help":storage_help()
else:break_planner()
