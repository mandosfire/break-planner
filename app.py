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
