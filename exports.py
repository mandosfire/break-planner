"""Colorblind-accessible, dependency-light PNG and CSV exports (Pillow only)."""
from __future__ import annotations
from collections import defaultdict
from datetime import timedelta
from io import BytesIO, StringIO
from zipfile import ZipFile, ZIP_DEFLATED
import csv
import math
import os
import re

from PIL import Image, ImageDraw, ImageFont

# Okabe–Ito-inspired colors; every break is also labeled in plain text.
COLORS={"Short":"#0072B2", "Meal":"#E69F00", "WB20":"#009E73", "WB70":"#9D4E96"}
LABELS={"Short":"S · Short", "Meal":"M · Meal", "WB20":"W20 · Wellness", "WB70":"W70 · Wellness"}
DARK="#16283C"
MUTED="#596B7A"
LIGHT="#EFF4F8"
WHITE="#FFFFFF"
FONT_REGULAR="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(size,bold=False):
    path=FONT_BOLD if bold else FONT_REGULAR
    try:return ImageFont.truetype(path,size)
    except OSError:return ImageFont.load_default()


def fmt(dt):
    return dt.strftime("%H:%M")


def slug(value):
    result=re.sub(r"[^\w\-]+","_",str(value or ""),flags=re.UNICODE).strip("_")
    return result or "NA"


def name_for(kind,payload,extension="png"):
    return f"{slug(kind)}_{slug(payload['date'])}_{slug(payload['pu'])}_{slug(payload['shift_code'])}.{extension}"


def fit_text(draw,text,x,y,maxw,font_size=19,bold=False,fill=DARK):
    f=font(font_size,bold)
    original=str(text)
    while original and draw.textbbox((0,0),original,font=f)[2]>maxw:
        original=original[:-1]
    if original!=text: original=original.rstrip()+"…"
    draw.text((x,y),original,font=f,fill=fill)


def _png_bytes(im):
    result=BytesIO()
    im.save(result,format="PNG",optimize=True)
    return result.getvalue()


def generate_timetable_png(payload):
    schedule=payload["schedule"]
    shift_start=payload["shift_start_dt"]
    shift_end=payload["shift_end_dt"]
    names=sorted({b["Name"] for b in schedule},key=lambda s:s.casefold())
    W=3000; H=max(800,310+len(names)*70)
    im=Image.new("RGB",(W,H),WHITE)
    d=ImageDraw.Draw(im)
    d.rectangle((0,0,W,145),fill=DARK)
    d.text((55,32),"PU BREAK TIMETABLE",font=font(38,True),fill=WHITE)
    d.text((57,87),f"{payload['date']}   •   {payload['pu']}   •   {payload['shift_label']}",font=font(24),fill="#DCE6F2")
    if payload.get("tickets",0):
        d.text((2450,45),f"Ticket moderators: {payload['tickets']}",font=font(21,True),fill="#DCE6F2")
    legend_items=[("Short","Short · S"),("Meal","Meal · M"),("WB20","Wellness 20 · W20"),("WB70","Wellness 70 · W70")]
    legend_x=440
    for break_type,title in legend_items:
        d.rounded_rectangle((legend_x,164,legend_x+44,194),radius=6,fill=COLORS[break_type],outline=DARK,width=2)
        d.text((legend_x+57,166),title,font=font(21,True),fill=DARK)
        legend_x+=min(560,max(290,len(title)*17+145))
    x0,x1=450,W-80
    top=255
    total=(shift_end-shift_start).total_seconds()/60.0
    if total<=0: raise ValueError("Shift end must be later than shift start")
    label_y=top-37
    for minute in range(0,int(total)+1,30):
        x=x0+round(minute/total*(x1-x0))
        thick=(minute%60==0)
        d.line((x,top-6,x,H-28),fill="#B7C5D2" if thick else "#E1EAF1",width=3 if thick else 1)
        d.text((x-32,label_y),fmt(shift_start+timedelta(minutes=minute)),font=font(15,thick),fill=MUTED)
    for idx,name in enumerate(names):
        yy=top+idx*70
        if idx%2==0:d.rectangle((20,yy-1,W-32,yy+68),fill="#F5F8FA")
        d.line((22,yy+69,W-35,yy+69),fill="#CFD8E2",width=1)
        breaks=sorted([b for b in schedule if b["Name"]==name],key=lambda b:b["Start"])
        is_ticket=any(b.get("TicketModerator") for b in breaks)
        fit_text(d,name,38,yy+20,345 if is_ticket else 380,20,True)
        if is_ticket:
            d.rounded_rectangle((388,yy+18,430,yy+49),radius=6,fill=DARK)
            d.text((398,yy+22),"T",font=font(19,True),fill=WHITE)
        for b in breaks:
            l=x0+round((b["Start"]-shift_start).total_seconds()/60.0/total*(x1-x0))
            r=x0+round((b["Finish"]-shift_start).total_seconds()/60.0/total*(x1-x0))
            if r-l<7:continue
            kind=b["Resource"]; color=COLORS[kind]
            d.rounded_rectangle((l+2,yy+5,r-2,yy+62),radius=7,fill=color,outline=DARK,width=2)
            textcolor="#132132" if kind=="Meal" else WHITE
            label={"Short":"S","Meal":"M","WB20":"W20","WB70":"W70"}[kind]
            center=(l+r)/2
            for text,y,size in [(label,yy+7,15),(fmt(b["Start"]),yy+24,13),(fmt(b["Finish"]),yy+42,13)]:
                f=font(size,True)
                width=d.textbbox((0,0),text,font=f)[2]
                if width < r-l-6:d.text((int(center-width/2),y),text,font=f,fill=textcolor)
    d.text((45,H-23),"All times local · S=Short · M=Meal · W20/W70=Wellness · T=Ticket moderator · Start/end times inside each bar",font=font(16),fill=MUTED)
    return _png_bytes(im)


CIVIDIS=["#00204C","#31476B","#66696F","#9C8C63","#D5B748","#FDE653"]

def _hex_to_rgb(value):return tuple(int(value[i:i+2],16) for i in (1,3,5))
def _interp_color(percent):
    percent=max(0,min(1,float(percent)))
    u=percent*(len(CIVIDIS)-1)
    i=min(int(u),len(CIVIDIS)-2); frac=u-i
    a,b=_hex_to_rgb(CIVIDIS[i]),_hex_to_rgb(CIVIDIS[i+1])
    return tuple(round(x+(y-x)*frac) for x,y in zip(a,b))


def generate_heatmap_png(payload):
    schedule=payload["schedule"]
    start=payload["earliest_dt"].replace(minute=0,second=0,microsecond=0)
    limit=payload["final_dt"]
    hours=[]
    while start<limit:
        hours.append(start)
        start+=timedelta(hours=1)
    W=1940; cellw=135; cellh=76; x0=218; top=250
    H=max(550,top+len(hours)*cellh+110)
    im=Image.new("RGB",(W,H),WHITE)
    d=ImageDraw.Draw(im)
    d.rectangle((0,0,W,154),fill=DARK)
    d.text((52,28),"CONCURRENT BREAKS · 5-MINUTE VIEW",font=font(32,True),fill=WHITE)
    d.text((52,88),f"{payload['date']}   •   {payload['pu']}   •   {payload['shift_label']}   •   {fmt(payload['earliest_dt'])}–{fmt(payload['final_dt'])}",font=font(21),fill="#DBE8F4")
    counts=[]
    for h in hours:
        row=[]
        for m in range(0,60,5):
            t=h+timedelta(minutes=m)
            if not payload["earliest_dt"]<=t<payload["final_dt"]:
                row.append(None)
            else:
                row.append(sum(1 for b in schedule if b["Start"]<=t<b["Finish"]))
        counts.append(row)
    mx=max((v for row in counts for v in row if v is not None),default=1)
    for ci,m in enumerate(range(0,60,5)):
        d.text((x0+ci*cellw+32,top-40),f":{m:02d}",font=font(21,True),fill=DARK)
    for ri,h in enumerate(hours):
        y=top+ri*cellh
        d.text((32,y+23),h.strftime("%H:00"),font=font(22,True),fill=DARK)
        for ci,v in enumerate(counts[ri]):
            x=x0+ci*cellw
            if v is None:
                d.rectangle((x+1,y+1,x+cellw-3,y+cellh-3),fill=LIGHT,outline="#D2DEE8",width=1)
                continue
            color=_interp_color(v/max(mx,1))
            d.rectangle((x+1,y+1,x+cellw-3,y+cellh-3),fill=color,outline=WHITE,width=2)
            lum=(.2126*color[0]+.7152*color[1]+.0722*color[2])
            fg=DARK if lum>142 else WHITE
            f=font(29,True);txt=str(v)
            wt=d.textbbox((0,0),txt,font=f)[2]
            d.text((int(x+(cellw-wt)/2),y+17),txt,font=f,fill=fg)
    d.text((50,H-69),f"Peak concurrent breaks: {mx}   ·   Each cell shows the exact number of moderators on break",font=font(22,True),fill=DARK)
    d.text((50,H-38),"Numbers are the primary indicator; color is a secondary visual aid. Grey cells are outside the allowed break window.",font=font(16),fill=MUTED)
    return _png_bytes(im)


def moderator_message(payload,name):
    rows=sorted([b for b in payload["schedule"] if b["Name"]==name],key=lambda b:b["Start"])
    labels={"Short":"Short","Meal":"Meal","WB20":"Wellness 20","WB70":"Wellness 70"}
    lines=[f"{name} — {payload['date']} · {payload['pu']} · {payload['shift_label']}",f"Shift: {fmt(payload['shift_start_dt'])}–{fmt(payload['shift_end_dt'])}"]
    short_num=0
    for b in rows:
        kind=b["Resource"]
        if kind=="Short":
            short_num+=1
            tag=f"Short {short_num}"
        else:tag=labels[kind]
        lines.append(f"{tag}: {fmt(b['Start'])}–{fmt(b['Finish'])}")
    return "\n".join(lines)


def generate_individual_card(payload,name):
    breaks=sorted([b for b in payload["schedule"] if b["Name"]==name],key=lambda b:b["Start"])
    W,H=1300,850
    im=Image.new("RGB",(W,H),WHITE)
    d=ImageDraw.Draw(im)
    d.rectangle((0,0,W,165),fill=DARK)
    fit_text(d,name,60,32,1160,38,True,WHITE)
    d.text((62,92),f"{payload['date']}   •   {payload['pu']}   •   {payload['shift_label']}   •   {fmt(payload['shift_start_dt'])}–{fmt(payload['shift_end_dt'])}",font=font(22),fill="#E0EAF2")
    d.text((62,190),"MY BREAKS",font=font(27,True),fill=DARK)
    count=0
    for idx,b in enumerate(breaks):
        y=245+idx*108
        if idx%2==0:d.rounded_rectangle((52,y,1245,y+99),radius=13,fill=LIGHT)
        color=COLORS[b["Resource"]]
        d.rounded_rectangle((72,y+18,88,y+80),radius=6,fill=color)
        kind=b["Resource"]
        if kind=="Short":count+=1;desc=f"Short break {count}"
        elif kind=="Meal":desc="Meal break"
        elif kind=="WB20":desc="Wellness 20"
        else:desc="Wellness 70"
        d.text((112,y+30),desc,font=font(26,True),fill=DARK)
        timetxt=f"{fmt(b['Start'])} – {fmt(b['Finish'])}"
        d.text((810,y+30),timetxt,font=font(27,True),fill=DARK)
    d.text((60,799),"Times shown in your local shift timezone · Please follow the latest shared PU schedule",font=font(18),fill=MUTED)
    return _png_bytes(im)


def generate_cards_zip(payload):
    buff=BytesIO()
    names=sorted({b["Name"] for b in payload["schedule"]},key=str.casefold)
    with ZipFile(buff,"w",compression=ZIP_DEFLATED) as bundle:
        for i,name in enumerate(names,1):
            bundle.writestr(f"Individual_Breaks/{i:03d}_{slug(name)}.png",generate_individual_card(payload,name))
        bundle.writestr("Moderator_break_summaries.txt","\n\n".join(moderator_message(payload,name) for name in names).encode("utf-8"))
    return buff.getvalue()


def generate_csv(payload):
    out=StringIO()
    writer=csv.writer(out)
    writer.writerow(["Date","PU","Shift","Moderator","Break type","Start","End","Duration minutes","Ticket moderator"])
    for b in sorted(payload["schedule"],key=lambda x:(x["Name"].casefold(),x["Start"])):
        writer.writerow([payload["date"],payload["pu"],payload["shift_label"],b["Name"],b["Resource"],fmt(b["Start"]),fmt(b["Finish"]),int((b["Finish"]-b["Start"]).total_seconds()/60),bool(b.get("TicketModerator"))])
    return out.getvalue().encode("utf-8-sig")
