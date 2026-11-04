    st.markdown("#### Individual moderator communication")
    names=sorted({b["Name"] for b in payload["schedule"]},key=str.casefold)
    chosen=st.selectbox("Find moderator",names,key="individual_moderator")
    text=moderator_message(payload,chosen)
    st.code(text,language=None)
    st.caption("Use the copy icon above to send the moderator their five exact break times in your existing PU or individual chat. Moderators do not need access to the website.")
    st.download_button("Download this moderator's break card PNG",data=generate_individual_card(payload,chosen),file_name=f"Individual_{payload['date']}_{payload['pu']}_{re.sub('[^\\w-]+','_',chosen)}.png",mime="image/png")
    st.download_button("Download all individual break cards (ZIP)",data=generate_cards_zip(payload),file_name=name_for("Individual_Cards",payload,"zip"),mime="application/zip")
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
