"""Weekly WB70 workbook importer and durable versioned Google Sheets storage.

The workbook is a matrix: one day per three columns (shift label, moderator,
optional fixed WB70 start or optional duration such as '50 dk').
Only uploaded WB70 assignments, not generated break schedules, are persisted.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from io import BytesIO
from zipfile import ZipFile
import re
import uuid

from zoneinfo import ZoneInfo
from roster import (
    FLOOR_DATE, _excel_shared_strings, _excel_sheet_paths, _excel_rows,
    _spreadsheet, _worksheet, normalize_name, read_excel_date,
)

WB70_PUS = ('HLC', 'Agent', 'AZ', 'Oncall', 'KU')
SHEET_PUS = ('Oncall', 'AZ', 'Agent', 'HLC', 'KU')
WB70_HEADERS = ['snapshot_id', 'date', 'pu', 'shift', 'name', 'fixed_start', 'duration']
WB70_VERSION_HEADERS = ['snapshot_id', 'start_row', 'end_row', 'created_at', 'uploaded_by', 'record_count']
WB70_CONTROL_HEADERS = ['active_snapshot_id', 'changed_at', 'changed_by']


def _colnum(value):
    number = 0
    for char in value:
        number = number * 26 + ord(char) - ord('A') + 1
    return number


def _colname(number):
    out = ''
    while number:
        number, rem = divmod(number - 1, 26)
        out = chr(ord('A') + rem) + out
    return out


def _time_or_duration(raw):
    value = str(raw or '').strip().replace(',', '.')
    if not value:
        return '', ''
    dur = re.fullmatch(r'(\d{1,3})\s*(?:dk|dakika|mins?|minutes?)\.?', value, re.I)
    if dur:
        minutes = int(dur.group(1))
        if minutes < 5 or minutes % 5:
            raise ValueError(f'WB70 duration {value} must be a positive multiple of five minutes.')
        return '', str(minutes)
    hhmm = re.fullmatch(r'(\d{1,2}):(\d{2})', value) or (re.fullmatch(r'([1-9]\d?)\.(\d{2})', value) if '.' in value else None)
    if hhmm:
        hh, mm = map(int, hhmm.groups())
        if hh > 23 or mm > 59 or mm % 5:
            raise ValueError(f'WB70 fixed start {value} must be on a five-minute clock boundary.')
        return f'{hh:02d}:{mm:02d}', ''
    try:
        number = float(value)
    except ValueError:
        raise ValueError(f'Unrecognized WB70 time or duration: {raw!r}. Use HH:MM or "50 dk".')
    if not 0 <= number < 1:
        raise ValueError(f'Excel WB70 start time must be a fraction of a day: {value!r}.')
    mins = round(number * 24 * 60)
    if mins % 5:
        raise ValueError(f'WB70 start time {raw!r} does not fall on a five-minute boundary.')
    return f'{mins // 60:02d}:{mins % 60:02d}', ''


def parse_wb70_workbook(content):
    """Return (entries, date/PU coverage, report) from uploaded Excel workbook."""
    try:
        archive = ZipFile(BytesIO(content))
    except Exception as exc:
        raise ValueError('Please upload a valid Excel .xlsx WB70 workbook.') from exc
    entries = []
    coverage = set()
    report = []
    with archive:
        strings = _excel_shared_strings(archive)
        paths = _excel_sheet_paths(archive)
        known = [p for p in SHEET_PUS if p in paths]
        if not known:
            raise ValueError('No WB70 worksheets found (expected HLC, Agent, AZ, Oncall or KU).')
        for pu in known:
            matrix = _excel_rows(archive, paths[pu], strings)
            dates = []
            for col, value in matrix.get(4, {}).items():
                day = read_excel_date(value)
                if day and day >= FLOOR_DATE:
                    dates.append((_colnum(col), day.isoformat()))
            if not dates:
                report.append({'PU':pu,'Days':0,'WB70 entries':0})
                continue
            before = len(entries)
            for col, day in dates:
                coverage.add((day, pu))
                date_col, name_col, value_col = _colname(col), _colname(col+1), _colname(col+2)
                section = ''
                for line in range(5, max(matrix, default=5) + 1):
                    cells = matrix.get(line, {})
                    shift = str(cells.get(date_col, '')).strip().casefold()
                    if shift in ('morning', 'mid', 'night'):
                        section = shift.capitalize()
                    name = str(cells.get(name_col, '')).strip()
                    if not name:
                        continue
                    if not section:
                        raise ValueError(f'{pu} {day}: moderator {name} is outside a Morning/Mid/Night section.')
                    try:
                        fixed, duration = _time_or_duration(cells.get(value_col, ''))
                    except ValueError as exc:
                        raise ValueError(f'{pu} {day} {name}: {exc}') from exc
                    entries.append({'date':day,'pu':pu,'shift':section,'name':name,'fixed_start':fixed,'duration':duration})
            report.append({'PU':pu,'Days':len(dates),'WB70 entries':len(entries)-before})
    seen = set()
    for e in entries:
        key = (e['date'], e['pu'], e['shift'], normalize_name(e['name']))
        if key in seen:
            raise ValueError(f"Duplicate WB70 entry for {e['name']} on {e['date']} in {e['pu']} {e['shift']}.")
        seen.add(key)
    if not coverage:
        raise ValueError('No WB70 dates on or after 4 November 2026 were found in row 4.')
    return entries, coverage, report


def _active(book):
    control = _worksheet(book, 'WB70Control', WB70_CONTROL_HEADERS)
    row = control.row_values(2)
    return str(row[0]).strip() if row else ''


def _versions(book):
    return _worksheet(book, 'WB70Versions', WB70_VERSION_HEADERS).get_all_records()


def load_wb70_plan():
    book = _spreadsheet()
    active = _active(book)
    if not active:
        return [], {'snapshot_id':'','record_count':0}
    version = next((v for v in reversed(_versions(book)) if str(v.get('snapshot_id')) == active), None)
    if version is None:
        raise RuntimeError(f'WB70 snapshot {active} not found in WB70Versions.')
    lo, hi = int(version['start_row']), int(version['end_row'])
    vals = _worksheet(book, 'WB70Plan', WB70_HEADERS).get(f'A{lo}:G{hi}')
    out = []
    for row in vals:
        padded = row + [''] * (len(WB70_HEADERS)-len(row))
        item = dict(zip(WB70_HEADERS, padded))
        if item['snapshot_id'] == active:
            out.append({h: str(item[h]) for h in WB70_HEADERS if h != 'snapshot_id'})
    if len(out) != int(version['record_count']):
        raise RuntimeError('Incomplete WB70 snapshot; previous published version remains in history.')
    return out, dict(version)


def publish_wb70_plan(uploaded_entries, coverage, uploader, expected_snapshot=None):
    if not str(uploader).strip():
        raise ValueError('Enter WFM/admin name or initials before publishing.')
    coverage = set(coverage)
    if not coverage:
        raise ValueError('No WB70 dates were found.')
    for item in uploaded_entries:
        if item.get('pu') not in WB70_PUS or (item.get('date'), item.get('pu')) not in coverage:
            raise ValueError('WB70 file contains an invalid PU or coverage date.')
    book = _spreadsheet()
    previous = _active(book)
    if expected_snapshot is not None and previous != expected_snapshot:
        raise RuntimeError('WB70 assignments were updated by another user. Refresh before publishing.')
    existing, _ = load_wb70_plan() if previous else ([], {})
    merged = [r for r in existing if (r.get('date'), r.get('pu')) not in coverage] + [dict(r) for r in uploaded_entries]
    merged.sort(key=lambda r: (r['date'], r['pu'], r['shift'], normalize_name(r['name'])))
    if not merged:
        raise ValueError('The published WB70 plan cannot be empty.')
    snapshot = uuid.uuid4().hex
    stamp = datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(timespec='seconds')
    sheet = _worksheet(book, 'WB70Plan', WB70_HEADERS)
    values = [[snapshot] + [str(r.get(h, '') or '') for h in WB70_HEADERS[1:]] for r in merged]
    response = sheet.append_rows(values, value_input_option='RAW')
    update_range = (response or {}).get('updates', {}).get('updatedRange', '')
    match = re.search(r'[A-Z]+(\d+):[A-Z]+(\d+)$', update_range)
    if not match:
        raise RuntimeError('Cannot verify WB70 import range; current WB70 plan remains unchanged.')
    lo, hi = map(int, match.groups())
    if hi-lo+1 != len(merged):
        raise RuntimeError('WB70 upload row count does not match; active version was not changed.')
    _worksheet(book, 'WB70Versions', WB70_VERSION_HEADERS).append_row(
        [snapshot, lo, hi, stamp, uploader, len(merged)], value_input_option='RAW')
    _worksheet(book, 'WB70Control', WB70_CONTROL_HEADERS).update(
        range_name='A2:C2', values=[[snapshot, stamp, uploader]])
    return {'snapshot_id':snapshot,'record_count':len(merged)}


def list_wb70_versions():
    return _versions(_spreadsheet())


def restore_wb70_version(snapshot, user):
    book = _spreadsheet()
    if not any(str(v.get('snapshot_id')) == str(snapshot) for v in _versions(book)):
        raise ValueError('WB70 version not found.')
    stamp = datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(timespec='seconds')
    _worksheet(book, 'WB70Control', WB70_CONTROL_HEADERS).update(
        range_name='A2:C2', values=[[str(snapshot), stamp, str(user)]])


def match_wb70(entries, date_value, pu, shift, moderator_name):
    name = normalize_name(moderator_name)
    hits = [r for r in entries if r['date'] == date_value and r['pu'] == pu
            and r['shift'] == shift and normalize_name(r['name']) == name]
    if len(hits) > 1:
        raise ValueError(f'Duplicate WB70 assignments found for {moderator_name}.')
    return hits[0] if hits else None
