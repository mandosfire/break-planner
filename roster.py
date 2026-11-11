"""Read-only Excel roster importer and persistent, versioned Google Sheets roster store.
All roster records earlier than 2026-11-04 are rejected by design.
No generated break plans are saved to this backend.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import json
import os
import re
import unicodedata
import uuid
import xml.etree.ElementTree as ET

FLOOR_DATE = date(2026, 11, 4)
PU_SHEETS = {
    "BD Shifts Live Agents": "Agent",
    "BD Shifts RM&OnCall": "Oncall",
    "BD Shifts Live HLC": "HLC",
    "LIVE-KU-ROW-AGENT-HLC": "KU",
    "LIVE-OTHERLANG-ROW-AGENT-HLC": "AZ",
}
PUS = list(PU_SHEETS.values())
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKGREL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
TIMED_SHIFT_RE = re.compile(r"^\s*(\d{1,2}):?(\d{2})\s*[-–]\s*(\d{1,2}):?(\d{2})\s*$")
NONSHIFT_CODES = {"", "OFF", "ATT", "RM", "HLC", "SV", "ML", "Yİ", "Üİ", "AL", "SL", "PL", "UL", "LOA", "VL", "TR", "KU", "AZ", "TRAINING", "LEAVE", "SICK", "HOLIDAY", "ABSENCE", "N/A", "-"}
ROSTER_HEADERS = ["snapshot_id", "record_id", "date", "pu", "name", "email", "wd_id", "iex_id", "shift_code", "source_sheet", "source_row"]
VERSIONS_HEADERS = ["snapshot_id", "start_row", "end_row", "created_at", "uploaded_by", "description", "record_count"]
CONTROL_HEADERS = ["active_snapshot_id", "changed_at", "changed_by"]
SETTINGS_HEADERS = ["pu", "identity", "name", "wb70", "ticket", "wb70_duration", "fixed_wb70", "updated_at"]


def normalize_name(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold().replace("ı", "i"))
    return " ".join("".join(ch for ch in normalized if not unicodedata.combining(ch)).split())


def person_identity(record):
    email = str(record.get("email", "")).strip().casefold()
    if "@" in email:
        return "email:" + email
    return "name:" + normalize_name(record.get("name", ""))


def person_overlap_key(record):
    # Email is most reliable; name is an independent secondary match in validate_roster.
    return person_identity(record)


def read_excel_date(value):
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)) or re.fullmatch(r"\d{5}(?:\.\d+)?", str(value)):
        try:
            return (date(1899, 12, 30) + timedelta(days=int(float(value))))
        except Exception:
            return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    return None


def parse_shift_code(value):
    code = str(value or "").strip().upper().replace(" ", "")
    aliases = {"M": "0730-1630", "D": "1500-0000", "N": "2330-0800"}
    code = aliases.get(code, code)
    m = TIMED_SHIFT_RE.match(code)
    if not m:
        return None
    sh, sm, eh, em = map(int, m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    duration = (eh * 60 + em - (sh * 60 + sm)) % 1440
    if not 240 <= duration <= 780:
        return None
    return f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"


def canonical_shift(code):
    parsed = parse_shift_code(code)
    if not parsed:
        return ""
    a, b = parsed
    return a.replace(":", "") + "-" + b.replace(":", "")


def shift_label(code):
    parsed = parse_shift_code(code)
    if not parsed:
        return str(code or "")
    a, b = parsed
    base = "Morning" if (a,b) in (("07:30","16:30"),("08:00","17:00"),("09:00","18:00")) else "Mid" if a == "15:00" and b == "00:00" else "Night" if a == "23:30" and b == "08:00" else "Shift"
    return f"{base} · {a}–{b}"


def _excel_shared_strings(archive):
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(elem.itertext()) for elem in root.findall(f"{NS}si")]


def _excel_sheet_paths(archive):
    book = ET.fromstring(archive.read("xl/workbook.xml"))
    relroot = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {}
    for relation in relroot:
        ident = relation.get("Id")
        target = relation.get("Target", "")
        if target.startswith("/"):
            filename = target.lstrip("/")
        else:
            filename = "xl/" + target.lstrip("/")
        targets[ident] = filename
    paths = {}
    for sheet in book.findall(f".//{NS}sheet"):
        identifier = sheet.get(REL + "id")
        if identifier and identifier in targets:
            paths[sheet.get("name")] = targets[identifier]
    return paths


def _excel_rows(archive, filename, strings):
    root = ET.fromstring(archive.read(filename))
    data = root.find(f"{NS}sheetData")
    if data is None:
        return {}
    rows = {}
    for row in data.findall(f"{NS}row"):
        line = int(row.get("r", "0"))
        vals = {}
        for cell in row.findall(f"{NS}c"):
            ref = cell.get("r", "")
            col_match = re.match(r"[A-Z]+", ref)
            if not col_match:
                continue
            raw = cell.find(f"{NS}v")
            inline = cell.find(f"{NS}is")
            if raw is not None and raw.text is not None:
                value = raw.text
            elif inline is not None:
                value = "".join(inline.itertext())
            else:
                continue
            if cell.get("t") == "s":
                try:
                    value = strings[int(value)]
                except (ValueError, IndexError):
                    continue
            vals[col_match.group()] = str(value).strip()
        if vals:
            rows[line] = vals
    return rows


def parse_workbook(file_content):
    """Parse only 5 official PU worksheets; returns assignments + per-sheet diagnostics."""
    assignments, report = [], []
    try:
        z = ZipFile(BytesIO(file_content))
    except Exception as exc:
        raise ValueError("Please upload a valid .xlsx Excel schedule.") from exc
    with z:
        strings = _excel_shared_strings(z)
        paths = _excel_sheet_paths(z)
        missing = [name for name in PU_SHEETS if name not in paths]
        if missing:
            raise ValueError("Missing required PU worksheet(s): " + ", ".join(missing))
        for sheet_name, pu in PU_SHEETS.items():
            rows = _excel_rows(z, paths[sheet_name], strings)
            date_headers = []
            for col, value in rows.get(8, {}).items():
                if col in ("A", "B", "C", "D"):
                    continue
                day = read_excel_date(value)
                if day and day >= FLOOR_DATE:
                    date_headers.append((col, day))
            if not date_headers:
                report.append({"PU":pu,"Worksheet":sheet_name,"First date":"—","Last date":"—","Dates":0,"Entries":0,"Timed shifts":0})
                continue
            count = 0
            timed = 0
            for line, values in rows.items():
                if line < 10:
                    continue
                name = values.get("D", "").strip()
                email = values.get("C", "").strip()
                wd_id = values.get("B", "").strip()
                iex_id = values.get("A", "").strip()
                # Other content in worksheets (legends, notes, summary rows) is not a moderator record.
                if not name or not ("@" in email or wd_id.isdigit() or iex_id.isdigit()):
                    continue
                for col, day in date_headers:
                    raw_code = values.get(col, "").strip()
                    if not raw_code:
                        continue
                    recognized = parse_shift_code(raw_code)
                    code = canonical_shift(raw_code) if recognized else raw_code.upper()
                    identity = email.casefold() if email else normalize_name(name)
                    key = f"{pu}|{day.isoformat()}|{line}|{identity}"
                    entry = {
                        "record_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
                        "date": day.isoformat(),
                        "pu": pu,
                        "name": name,
                        "email": email,
                        "wd_id": wd_id,
                        "iex_id": iex_id,
                        "shift_code": code,
                        "source_sheet": sheet_name,
                        "source_row": str(line),
                    }
                    assignments.append(entry)
                    count += 1
                    timed += bool(recognized)
            report.append({"PU":pu,"Worksheet":sheet_name,"First date":min(d for _,d in date_headers).isoformat(),"Last date":max(d for _,d in date_headers).isoformat(),"Dates":len(date_headers),"Entries":count,"Timed shifts":timed})
    if not assignments:
        raise ValueError("No assignments on or after 2026-11-04 were found in the five selected worksheets.")
    return assignments, report


def validate_roster(rows):
    """Return list of actionable flags (critical duplicates and warnings)."""
    issues = []
    timed_groups = defaultdict(list)
    ids_by_wd = defaultdict(set)
    for row in rows:
        day = read_excel_date(row.get("date"))
        if day is None or day < FLOOR_DATE:
            issues.append({"Severity":"Error","Date":row.get("date",""),"PU":row.get("pu",""),"Issue":"Invalid/out-of-scope date","Moderator":row.get("name","")})
        if row.get("pu") not in PUS:
            issues.append({"Severity":"Error","Date":row.get("date",""),"PU":row.get("pu",""),"Issue":"Unrecognized Power Unit","Moderator":row.get("name","")})
        if not str(row.get("name", "")).strip():
            issues.append({"Severity":"Error","Date":row.get("date",""),"PU":row.get("pu",""),"Issue":"Missing moderator name","Moderator":"—"})
        code = str(row.get("shift_code", "")).strip().upper()
        wd = str(row.get("wd_id", "")).strip()
        if wd and str(row.get("name", "")).strip():
            ids_by_wd[wd].add((normalize_name(row.get("name")), str(row.get("email", "")).casefold()))
        if parse_shift_code(code):
            timed_groups[row.get("date", "")].append(row)
            if not str(row.get("email", "")).strip() and not wd:
                issues.append({"Severity":"Warning","Date":row.get("date",""),"PU":row.get("pu",""),"Issue":"Active moderator missing email/WD ID","Moderator":row.get("name","")})
        elif code not in NONSHIFT_CODES:
            issues.append({"Severity":"Warning","Date":row.get("date",""),"PU":row.get("pu",""),"Issue":f"Unrecognized shift/status code: {code}","Moderator":row.get("name","")})
    seen_issues=set()
    for day, group in timed_groups.items():
        for i, a in enumerate(group):
            for b in group[i+1:]:
                same_email = a.get("email") and b.get("email") and str(a["email"]).casefold() == str(b["email"]).casefold()
                same_name = normalize_name(a.get("name")) and normalize_name(a.get("name")) == normalize_name(b.get("name"))
                if not (same_email or same_name):
                    continue
                names = a["name"]
                locations = sorted([f"{a['pu']} ({a['shift_code']})", f"{b['pu']} ({b['shift_code']})"])
                description = ("Active in multiple PUs" if a["pu"] != b["pu"] else "Duplicate active assignment in same PU") + ": " + " + ".join(locations)
                identifier=(day,normalize_name(names),description)
                if identifier in seen_issues:
                    continue
                seen_issues.add(identifier)
                issues.append({"Severity":"Error","Date":day,"PU":" / ".join(sorted(set([a["pu"],b["pu"]]))),"Issue":description,"Moderator":names})
    for wd, identities in ids_by_wd.items():
        if len(identities) > 1:
            names = sorted(x[0] for x in identities)
            # This ID issue is informational: WD IDs cannot be used as a sole identity key.
            issues.append({"Severity":"Warning","Date":"Multiple","PU":"Multiple","Issue":f"WD ID {wd} is shared by different identities","Moderator":", ".join(names[:3])})
    return sorted(issues, key=lambda x:(x["Severity"]!="Error", x["Date"], x["PU"], x["Moderator"]))


def affected_conflicts(rows, selected_date, selected_pu, selected_shift):
    """Duplicate active people assigned to >1 PU or twice in same PU on this date."""
    selected_ids = {person_identity(r) for r in rows if r.get("date")==selected_date and r.get("pu")==selected_pu and canonical_shift(r.get("shift_code"))==selected_shift}
    selected_names = {normalize_name(r.get("name")) for r in rows if r.get("date")==selected_date and r.get("pu")==selected_pu and canonical_shift(r.get("shift_code"))==selected_shift}
    conflicts=[]
    for flag in validate_roster(rows):
        if flag["Severity"]!="Error" or flag["Date"]!=selected_date or not flag["Issue"].startswith(("Active in multiple PUs", "Duplicate active assignment")):
            continue
        if normalize_name(flag["Moderator"]) in selected_names:
            conflicts.append(flag)
    return conflicts


def _secret_info():
    import streamlit as st
    from google.oauth2.service_account import Credentials
    raw = {}
    if "gcp_service_account" in st.secrets:
        raw = dict(st.secrets["gcp_service_account"])
    else:
        env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if env:
            raw = json.loads(env)
    if not raw:
        raise RuntimeError("Missing [gcp_service_account] in Streamlit Secrets.")
    raw["private_key"] = str(raw.get("private_key", "")).replace("\\n", "\n").strip() + "\n"
    if not raw["private_key"].startswith("-----BEGIN PRIVATE KEY-----"):
        raise RuntimeError("Your Google service-account private_key must include its BEGIN/END PRIVATE KEY PEM lines.")
    return Credentials.from_service_account_info(raw, scopes=["https://www.googleapis.com/auth/spreadsheets"])


def _spreadsheet():
    import streamlit as st
    import gspread
    identifier = str(st.secrets.get("break_planner", {}).get("spreadsheet_id", "")).strip() or os.environ.get("BREAK_PLANNER_SPREADSHEET_ID", "")
    if not identifier:
        raise RuntimeError("Missing [break_planner] spreadsheet_id in Secrets.")
    return gspread.authorize(_secret_info()).open_by_key(identifier)


def _worksheet(book, title, headers):
    import gspread
    try:
        tab = book.worksheet(title)
    except gspread.WorksheetNotFound:
        tab = book.add_worksheet(title=title, rows=1000, cols=max(14,len(headers)))
    first = tab.row_values(1)
    if not first:
        tab.update(range_name="A1", values=[headers])
    elif first[:len(headers)] != headers:
        raise RuntimeError(f"Worksheet '{title}' has unexpected columns. Do not overwrite it manually; expected {headers}.")
    return tab


def connection_check():
    book = _spreadsheet()
    for name, header in (("Roster",ROSTER_HEADERS),("RosterVersions",VERSIONS_HEADERS),("RosterControl",CONTROL_HEADERS),("ModeratorSettings",SETTINGS_HEADERS)):
        _worksheet(book,name,header)
    return book.title


def _load_versions(book):
    ws = _worksheet(book,"RosterVersions",VERSIONS_HEADERS)
    return ws.get_all_records()


def active_snapshot_id(book):
    ws=_worksheet(book,"RosterControl",CONTROL_HEADERS)
    row=ws.row_values(2)
    return str(row[0]).strip() if row else ""


def load_roster():
    """Load only active snapshot range; old revisions stay recoverable."""
    book=_spreadsheet()
    active=active_snapshot_id(book)
    if not active:
        return [], {"snapshot_id":"","record_count":0}
    versions=_load_versions(book)
    version=next((r for r in reversed(versions) if str(r.get("snapshot_id"))==active),None)
    if version is None:
        raise RuntimeError(f"Active roster version {active} is missing from RosterVersions.")
    lo,hi=int(version["start_row"]),int(version["end_row"])
    sheet=_worksheet(book,"Roster",ROSTER_HEADERS)
    values=sheet.get(f"A{lo}:K{hi}")
    records=[]
    for cells in values:
        padded=cells+[""]*(len(ROSTER_HEADERS)-len(cells))
        record=dict(zip(ROSTER_HEADERS,padded))
        if record.get("snapshot_id")==active:
            records.append({k:record.get(k, "") for k in ROSTER_HEADERS if k!="snapshot_id"})
    if len(records)!=int(version["record_count"]):
        raise RuntimeError("Incomplete roster snapshot in Google Sheets. Previous active snapshot remains available in RosterVersions.")
    return records, dict(version)


def publish_roster(rows, uploader, description="", expected_snapshot=None):
    """Append full roster snapshot, verify rows, then switch pointer last (safe publishing)."""
    if not str(uploader).strip():
        raise ValueError("Enter a WFM/admin name or initials before publishing.")
    clean=[]
    for r in rows:
        day=read_excel_date(r.get("date"))
        if day is None or day < FLOOR_DATE:
            raise ValueError("Roster cannot contain dates earlier than 2026-11-04 or invalid date values.")
        if r.get("pu") not in PUS:
            raise ValueError("Invalid PU in roster: " + str(r.get("pu")))
        if not str(r.get("name", "")).strip():
            raise ValueError("Missing moderator name. Fix it in the roster editor before publishing.")
        r=dict(r)
        r["date"]=day.isoformat()
        r["shift_code"]=canonical_shift(r.get("shift_code")) or str(r.get("shift_code", "")).strip().upper()
        r["record_id"]=str(r.get("record_id") or uuid.uuid4())
        clean.append(r)
    if not clean:
        raise ValueError("Cannot publish an empty roster.")
    book=_spreadsheet()
    previous=active_snapshot_id(book)
    if expected_snapshot is not None and previous!=expected_snapshot:
        raise RuntimeError("Another user published newer roster data. Refresh the roster before trying to save corrections.")
    stamp=datetime.now(ZoneInfo("Europe/Istanbul")).isoformat(timespec="seconds")
    snapshot_id=uuid.uuid4().hex
    ws=_worksheet(book,"Roster",ROSTER_HEADERS)
    tab_rows=[[snapshot_id]+[str(r.get(h, "") or "") for h in ROSTER_HEADERS[1:]] for r in clean]
    response=ws.append_rows(tab_rows,value_input_option="RAW")
    update_range=(response or {}).get("updates",{}).get("updatedRange", "")
    m=re.search(r"[A-Z]+(\d+):[A-Z]+(\d+)$",update_range)
    if not m:
        raise RuntimeError("Could not verify uploaded roster row range; current roster was not changed.")
    lo,hi=int(m.group(1)),int(m.group(2))
    if hi-lo+1!=len(clean):
        raise RuntimeError("Roster upload row count did not match; current roster was not changed.")
    versions=_worksheet(book,"RosterVersions",VERSIONS_HEADERS)
    versions.append_row([snapshot_id,lo,hi,stamp,uploader,description,len(clean)],value_input_option="RAW")
    control=_worksheet(book,"RosterControl",CONTROL_HEADERS)
    control.update(range_name="A2:C2",values=[[snapshot_id,stamp,uploader]])
    return {"snapshot_id":snapshot_id,"record_count":len(clean),"created_at":stamp,"uploaded_by":uploader}


def list_roster_versions():
    return _load_versions(_spreadsheet())


def restore_roster_version(snapshot_id,uploader):
    book=_spreadsheet()
    version=next((r for r in _load_versions(book) if str(r.get("snapshot_id"))==str(snapshot_id)),None)
    if not version:
        raise ValueError("Requested roster version not found")
    stamp=datetime.now(ZoneInfo("Europe/Istanbul")).isoformat(timespec="seconds")
    _worksheet(book,"RosterControl",CONTROL_HEADERS).update(range_name="A2:C2",values=[[str(snapshot_id),stamp,uploader]])


def load_moderator_settings():
    sheet=_worksheet(_spreadsheet(),"ModeratorSettings",SETTINGS_HEADERS)
    result={}
    for item in sheet.get_all_records():
        pu=str(item.get("pu", ""))
        identity=str(item.get("identity", ""))
        if pu and identity:
            result[(pu,identity)]={
                "wb70":str(item.get("wb70", "")).lower() in ("true","yes","1"),
                "ticket":str(item.get("ticket", "")).lower() in ("true","yes","1"),
                "wb70_duration":str(item.get("wb70_duration", "") or ""),
                "fixed_wb70":str(item.get("fixed_wb70", "") or ""),
            }
    return result


def upsert_moderator_settings(pu, rows):
    book=_spreadsheet()
    sheet=_worksheet(book,"ModeratorSettings",SETTINGS_HEADERS)
    existing=sheet.get_all_records()
    data={}
    for item in existing:
        key=(str(item.get("pu", "")),str(item.get("identity", "")))
        if all(key): data[key]=dict(item)
    timestamp=datetime.now(ZoneInfo("Europe/Istanbul")).isoformat(timespec="seconds")
    for row in rows:
        identity=str(row["identity"])
        key=(pu,identity)
        data[key]={"pu":pu,"identity":identity,"name":str(row.get("name", "")),"wb70":str(bool(row.get("wb70"))),"ticket":str(bool(row.get("ticket"))),"wb70_duration":str(row.get("wb70_duration", "")),"fixed_wb70":str(row.get("fixed_wb70", "")),"updated_at":timestamp}
    values=[SETTINGS_HEADERS]+[[str(row.get(h, "")) for h in SETTINGS_HEADERS] for row in data.values()]
    sheet.update(range_name=f"A1:H{len(values)}",values=values)
    return len(rows)
