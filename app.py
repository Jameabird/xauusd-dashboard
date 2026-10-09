"""
Dashboard บอท XAUUSD — อ่านข้อมูลที่ bot.py ส่งขึ้น MongoDB (ดู monitor.py)

รันในเครื่อง:   streamlit run dashboard/app.py   (อ่านค่าจาก env หรือ .streamlit/secrets.toml)
ขึ้นออนไลน์:     Streamlit Community Cloud (เปิดดูได้ทุกคนที่มีลิงก์ — ปุ่มควบคุมยังต้องใช้ PIN) → Secrets:
                  MONGODB_URI = "mongodb://dashboard_reader:...@.../"      (user อ่านอย่างเดียว)
                  # ปุ่มควบคุมบอท (ไม่บังคับ):
                  MONGODB_CONTROL_URI = "mongodb://dashboard_control:...@.../"  (readWrite เฉพาะ db xauusd_bot)
                  CONTROL_PIN = "..."
"""
import hmac
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from pymongo import DESCENDING, MongoClient
from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

TH = timezone(timedelta(hours=7))
SERVER_TZ = "America/New_York"  # MetaQuotes-Demo: เวลาเซิร์ฟเวอร์ = เวลานิวยอร์ก + 7 ชม. (UTC+2/+3 ตาม DST สหรัฐ ไม่ใช่ยุโรป)
OFFLINE_AFTER_S = 60  # บอทอัปเดตทุก ~10 วิ ถ้าเงียบเกินนี้ถือว่าหยุด/คอมดับ/เน็ตหลุด
CALENDAR_STALE_MIN = 180
MAX_PIN_TRIES = 5
REAL_STOP_BALANCE, REAL_TARGET_BALANCE, REAL_TARGET_STEP = 20.0, 50.0, 10.0  # บัญชีจริง: ตรงกับ BOT_REAL_STOP_BALANCE / BOT_REAL_TARGET_BALANCE / BOT_REAL_TARGET_STEP ใน scripts/run_bot_exreal_*.bat (อัปเดต 2026-10-08)
REAL_DAILY_LOSS_PCT = 60.0  # BOT_DAILY_LOSS_PCT ของบอทจริง (% ของ balance)
UP, DOWN, GOLD, MA_FAST, MA_SLOW, MUTED = "#3cc08e", "#f0675c", "#d4a017", "#6b9cff", "#a3aab6", "#8a919c"
ACTIONS = {
    "follow_trend": ("📈 เข้าตามเทรนด์ตอนนี้", "เข้าตามทิศ MA ปัจจุบันทันที ไม่ต้องรอ MA ตัดใหม่ — ยังผ่าน ADX / ข่าว / ลิมิต "
                     "และมี SL เหมือนปกติ ถ้าตอนนี้ติดเงื่อนไข บอทจะรอเข้าเองตอนแท่งปิด"),
    "pause": ("⏸ หยุดเข้าไม้ใหม่", "บอทจะไม่เปิดไม้ใหม่ ไม้ที่ถืออยู่ยังมี SL และออกตามสัญญาณปกติ"),
    "resume": ("▶ กลับมาเข้าไม้ตามปกติ", "บอทกลับมาเปิดไม้ตามสัญญาณ"),
    "reset": ("🔄 รีเซ็ตเงื่อนไข", "ล้างสัญญาณที่รอเข้า (armed) + สิทธิ์ re-entry และกลับมาเข้าไม้ปกติ — ไม้ที่เปิดอยู่ไม่ถูกแตะ "
                "บอทจะรอสัญญาณ MA ตัดครั้งใหม่"),
    "close_all": ("⛔ ปิดทุกไม้ + หยุดเข้าไม้", "ปิดไม้ของบอททั้งหมดที่ราคาตลาดทันที แล้วหยุดเข้าไม้ใหม่"),
}

st.set_page_config(page_title="XAUUSD Bot", page_icon="📈", layout="wide")


# ---------- ตัวช่วย ----------
def secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets[name]).strip()  # str(): รองรับ PIN ที่ใส่เป็นตัวเลขไม่มี "..." ใน TOML
    except Exception:
        return os.getenv(name, default)


@st.cache_resource
def get_db(uri_name: str):
    client = MongoClient(secret(uri_name), serverSelectionTimeoutMS=5000, tz_aware=True)
    return client[secret("MONGODB_DB", "xauusd_bot")]


def th_time(dt: datetime) -> str:
    return dt.astimezone(TH).strftime("%d/%m %H:%M:%S")


def server_to_th(values, utc: bool = False) -> pd.Series:
    """เวลาเซิร์ฟเวอร์ MT5 (ข้อความ) → เวลาไทยแบบไม่มี timezone (ให้กราฟแสดงตามนั้นตรงๆ)"""
    t = pd.to_datetime(pd.Series(values), errors="coerce")
    if utc:  # Exness: เวลาเซิร์ฟเวอร์ = UTC
        return t.dt.tz_localize("UTC").dt.tz_convert("Asia/Bangkok").dt.tz_localize(None)
    t = (t - pd.Timedelta(hours=7)).dt.tz_localize(SERVER_TZ, ambiguous=False, nonexistent="shift_forward")
    return t.dt.tz_convert("Asia/Bangkok").dt.tz_localize(None)


def until(dt: datetime, now: datetime) -> str:
    sec = (dt - now).total_seconds()
    if sec <= 0:
        return "กำลังออก"
    d, rem = divmod(int(sec), 86400)
    h, m = divmod(rem // 60, 60)
    return f"{d} วัน {h} ชม." if d else (f"{h} ชม. {m} นาที" if h else f"{m} นาที")


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} วินาทีที่แล้ว"
    if seconds < 5400:
        return f"{seconds / 60:.0f} นาทีที่แล้ว"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} ชั่วโมงที่แล้ว"
    return f"{seconds / 86400:.0f} วันที่แล้ว"


def streak(signs: list[bool], value: bool) -> int:
    best = run = 0
    for s in signs:
        run = run + 1 if s == value else 0
        best = max(best, run)
    return best


# ---------- เชื่อมต่อ ----------
if not secret("MONGODB_URI"):
    st.error("ยังไม่ได้ตั้ง MONGODB_URI — ใส่ใน Secrets ของ Streamlit หรือ environment variable")
    st.stop()
db = get_db("MONGODB_URI")

# บอทที่ปลดแล้ว (2026-10-06: ไม่เคยเข้าไม้จากสัญญาณ) — ซ่อนจากทุกหน้า ข้อมูลยังอยู่ใน DB (ประวัติไม้ที่ปิดแล้วยังนับในกำไรรวม)
RETIRED_BOT_IDS = ["10012984221-20261003", "10012984221-20261004", "10012984221-20261006", "414406827-20261006",  # main, re3, bo, ex-bo
                   "10012984221-20261005", "10012984221-20261007", "10012984221-20261008", "10012984221-20261009"]  # hf, msc, rsc, bsc (ปลด 2026-10-08)


class _VisibleStatus:
    def __init__(self, coll):
        self._c = coll

    def _q(self, q):
        # 2026-10-08: เทรดบน MetaQuotes เดโมอย่างเดียว — ซ่อนบอท Exness (เดโม/จริง) ทั้งหมด ข้อมูลยังอยู่ใน DB
        return {"$and": [q or {}, {"_id": {"$nin": RETIRED_BOT_IDS}}, {"server": {"$not": {"$regex": "^Exness", "$options": "i"}}}]}

    def find(self, q=None, *a, **k):
        return self._c.find(self._q(q), *a, **k)

    def find_one(self, q=None, *a, **k):
        return self._c.find_one(self._q(q), *a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


class _VisibleDB:
    def __init__(self, database):
        self._d = database
        self.status = _VisibleStatus(database.status)

    def __getattr__(self, name):
        return getattr(self._d, name)

    def __getitem__(self, name):
        return self.status if name == "status" else self._d[name]

db = _VisibleDB(db)
try:
    _st = list(db.status.find({}, {"_id": 1, "profile": 1, "config.timeframe": 1, "reentry": 1, "server": 1}))
    server_by_bot = {d["_id"]: str(d.get("server") or "") for d in _st}
    real_ids = {d["_id"] for d in _st if "real" in str(d.get("server") or "").lower() and str(d.get("server") or "").lower().startswith("exness")}
    bot_ids = [d["_id"] for d in _st if d["_id"] not in real_ids]
    def _pname(prof: str) -> str:
        acct, base = (("Exness จริง · ", prof[7:]) if prof.startswith("exreal-") else ("Exness · ", prof[3:]) if prof.startswith("ex-") else ("", prof))
        return acct + PROFILE_NAMES.get(base, base)
    PROFILE_NAMES = {"main": "บอทหลัก", "re3": "re3 · re-entry", "hf": "hf · ความถี่สูง (ทดลอง)", "bo": "bo · breakout H4", "msc": "msc · ฝึก scalp MA5/13", "rsc": "rsc · ฝึก scalp MA3/21+re", "bsc": "bsc · ฝึก scalp breakout", "m30b": "m30b · breakout M30 (ทดสอบ)", "m15b": "m15b · breakout M15 (ทดสอบ)", "m15sq": "m15sq · squeeze M15", "m15roc": "m15roc · momentum M15", "m30sq": "m30sq · squeeze M30", "m30mom": "m30mom · momentum M30", "m1run": "m1run · แท่งสีเดียวกัน M1 (สำรวจ)", "m5run": "m5run · แท่งสีเดียวกัน M5 (สำรวจ)", "m10run": "m10run · แท่งสีเดียวกัน M10 (สำรวจ)", "m15run": "m15run · แท่งสีเดียวกัน M15 (สำรวจ)", "h1roc": "h1roc · โมเมนตัม H1 (สำรวจ)", "h4bo": "h4bo · breakout H4 (สำรวจ)"}
    bot_labels = {d["_id"]: f"{_pname(d.get('profile', 'main'))} · "
                            f"{(d.get('config') or {}).get('timeframe', '')} ({d['_id'].split('-')[-1]})" for d in _st}
except OperationFailure as e:
    if e.code in (18, 8000):  # AuthenticationFailed (Atlas ส่ง 8000 "bad auth")
        st.error("ล็อกอิน MongoDB ไม่ผ่าน — ชื่อ user หรือรหัสใน MONGODB_URI (Secrets) ไม่ตรงกับใน Atlas "
                 "(Database Access) เช็คว่าแทนที่ <db_password> แล้ว ไม่มี < > เหลืออยู่ และ user ถูกสร้างเสร็จแล้ว")
    else:
        st.error(f"user นี้ไม่มีสิทธิ์อ่านข้อมูล (code {e.code}) — ใน Atlas ให้ role 'Only read any database'")
    st.stop()
except ServerSelectionTimeoutError:
    st.error("ต่อ MongoDB ไม่ได้ — เช็ค Network Access ใน Atlas ว่ามี 0.0.0.0/0 และ host ใน MONGODB_URI ถูกต้อง")
    st.stop()
if not bot_ids:
    st.info("ยังไม่มีข้อมูลจากบอท — รัน bot.py ที่มี MONGODB_URI ใน .env แล้วรอสักครู่")
    st.stop()

# บอทอ้างอิงสำหรับข่าว/ตลาด = m15b บน MetaQuotes (ไม่มีตัวเลือกบอทในแถบข้างแล้ว — หน้าแสดงทั้งทีม)
bot_id = next((d["_id"] for d in _st if d.get("profile") == "m15b" and "metaquotes" in str(d.get("server", "")).lower()), bot_ids[0])
days, n_bars = 30, 120


# 2026-10-08 (ผู้ใช้สั่ง): ถอดแผงควบคุม/ปุ่มสั่งเข้า-ออกไม้ออก — dashboard ดูข้อมูลอย่างเดียว


# ---------- ส่วนแสดงผล ----------
def header(s: dict, now: datetime) -> None:
    age = (now - s["updated_at"]).total_seconds()
    online = s.get("running", False) and age < OFFLINE_AFTER_S
    cfg = s.get("config", {})
    left, right = st.columns([3, 2], vertical_alignment="bottom")
    left.title(f"{s.get('symbol', 'XAUUSD')} Bot")
    left.caption(f"{s.get('server')} · login {s.get('login')} · {cfg.get('timeframe')} "
                 f"MA{cfg.get('fast')}/{cfg.get('slow')} · SL {cfg.get('sl_atr')}×ATR · ADX ≥ {cfg.get('adx_min')} · "
                 + (f"เสี่ยง {cfg['risk_pct']}%/ไม้ (สูงสุด {cfg.get('max_lot')} lot) · " if cfg.get("risk_pct") else "")
                 + (f"บล็อกข่าว {cfg.get('news_set')} ±{cfg.get('news_before')} นาที" if cfg.get("news_filter") else "ไม่กรองข่าว"))
    if online:
        right.markdown(f"### :green-badge[● ONLINE]" + (" :orange-badge[⏸ หยุดเข้าไม้]" if s.get("paused") else "")
                       + f"\nอัปเดต {ago(age)} · {th_time(s['updated_at'])} (เวลาไทย)")
    else:
        why = "บอทถูกหยุด" if not s.get("running", False) else "ไม่ได้รับสัญญาณจากบอท (คอมดับ / เน็ตหลุด / โปรแกรมค้าง)"
        right.markdown(f"### :red-badge[● OFFLINE]\n{why} · ล่าสุด {ago(age)}")
    if online and not s.get("algo_trading_on", True):
        st.warning("ปุ่ม Algo Trading ใน MT5 ปิดอยู่ — บอทส่งออเดอร์ไม่ได้")
    if s.get("paused"):
        st.warning("บอทหยุดเข้าไม้ใหม่อยู่ (สั่งจาก dashboard) — ใช้แผงควบคุมด้านซ้ายเพื่อกลับมาเข้าไม้")
    if s.get("halted_today"):
        st.error(f"วันนี้ถึงลิมิตขาดทุนรายวันแล้ว (${cfg.get('daily_loss_limit'):g}) — หยุดเข้าไม้ใหม่จนถึงพรุ่งนี้")
    cal_age = s.get("calendar_age_min")
    if online and cfg.get("news_filter") and (cal_age is None or cal_age > CALENDAR_STALE_MIN):
        st.error("ปฏิทินข่าวไม่อัปเดต — บอทจะไม่เปิดไม้ใหม่ เช็ค Service CalendarExport ใน MT5")


def what_bot_waits_for(s: dict, now: datetime) -> None:
    cfg, ind = s.get("config", {}), s.get("indicators") or {}
    f, sl_ = cfg.get("fast"), cfg.get("slow")
    with st.container(border=True):
        st.markdown("**บอทกำลังรออะไร**")
        if not ind:
            st.caption("ยังไม่มีข้อมูลตัวชี้วัด (รอบอทรันเวอร์ชันใหม่)")
            return
        gap, gap_atr, adx, adx_min = ind.get("gap") or 0, ind.get("gap_atr") or 0, ind.get("adx") or 0, cfg.get("adx_min", 0)
        above = ind.get("aligned") == 1
        pos = (s.get("positions") or [None])[0]
        if pos:
            exit_dir = "ลง" if pos["side"] == "BUY" else "ขึ้น"
            st.markdown(f"ถือ **{pos['side']}** อยู่ — จะออกเมื่อ MA{f} ตัด{exit_dir} MA{sl_} "
                        f"(ตอนนี้ห่าง **${abs(gap):,.2f} = {abs(gap_atr):.1f} ATR**) หรือราคาแตะ SL {pos['sl']:,.2f}")
        elif s.get("armed"):
            side = "BUY" if s["armed"] == 1 else "SELL"
            news = s.get("news_block_now")
            checks = [
                (ind.get("aligned") == s["armed"], f"MA{f} ยังอยู่{'เหนือ' if s['armed'] == 1 else 'ใต้'} MA{sl_}"),
                (adx >= adx_min, f"ADX {adx:.1f} ≥ {adx_min}"),
                (not news, f"ไม่อยู่ช่วงข่าว" + (f" (ตอนนี้: {news})" if news else "")),
                (not s.get("paused"), "ไม่ได้สั่งหยุดเข้าไม้"),
                (not s.get("halted_today"), "ยังไม่ถึงลิมิตขาดทุนวันนี้"),
                (s.get("entries_today", 0) < cfg.get("max_trades_per_day", 99), "ยังไม่ครบจำนวนไม้วันนี้"),
            ]
            st.markdown(f"มีสัญญาณ **{side}** ตั้งแต่ {s.get('armed_at')} รอให้ครบเงื่อนไข (เช็คตอนแท่งปิด):  \n"
                        + "  \n".join(f"{'✅' if ok else '❌'} {txt}" for ok, txt in checks))
        else:
            need = "ลง" if above else "ขึ้น"
            ok = adx >= adx_min
            st.markdown(f"รอ MA{f} ตัด{need} MA{sl_} — ตอนนี้ MA{f} อยู่{'เหนือ' if above else 'ใต้'} "
                        f"ห่าง **${abs(gap):,.2f} ({abs(gap_atr):.1f} ATR)**  \n"
                        f"ถ้าตัดตอนนี้ {'เข้าได้ทันที' if ok else 'ยังเข้าไม่ได้'}: ADX {adx:.1f} {'≥' if ok else '<'} {adx_min}")
        if s.get("next_bar_close"):
            nb = server_to_th([s["next_bar_close"]]).iloc[0]
            if pd.notna(nb):
                nb_utc = nb.tz_localize("Asia/Bangkok").tz_convert("UTC").to_pydatetime()
                left = "ตลาดปิดอยู่ — บอทจะเช็คเมื่อแท่งแรกหลังตลาดเปิดปิด" if nb_utc <= now else f"อีก {until(nb_utc, now)}"
                st.caption(f"บอทเช็คครั้งถัดไปตอนแท่ง {cfg.get('timeframe')} ปิด: {nb:%d/%m %H:%M} น. ({left})")
        c = st.columns(5)
        c[0].metric("ราคาปิดแท่งล่าสุด", f"{ind.get('close', 0):,.2f}")
        c[1].metric(f"MA{f}", f"{ind.get('ma_fast') or 0:,.2f}")
        c[2].metric(f"MA{sl_}", f"{ind.get('ma_slow') or 0:,.2f}")
        c[3].metric("ADX", f"{adx:.1f}", f"เกณฑ์ {adx_min}", delta_color="off", delta_arrow="off")
        c[4].metric("ATR", f"{ind.get('atr') or 0:,.2f}")


def overview_tab(s: dict, now: datetime) -> None:
    cfg, cur = s.get("config", {}), s.get("currency", "USD")
    m = st.columns(4)
    floating = s["equity"] - s["balance"]
    m[0].metric(f"Balance ({cur})", f"{s['balance']:,.2f}")
    m[1].metric(f"Equity ({cur})", f"{s['equity']:,.2f}", f"{floating:+,.2f} ลอย" if floating else None)
    limit = cfg.get("daily_loss_limit", 0)
    m[2].metric(f"กำไร/ขาดทุนวันนี้ ({cur})", f"{s['day_pnl']:+,.2f}")
    m[3].metric("ไม้วันนี้", f"{s['entries_today']} / {cfg.get('max_trades_per_day')}")
    alerts = s.get("alerts") or {}
    st.caption(f"เหลืออีก {limit + s['day_pnl']:,.2f} {cur} ก่อนถึงลิมิตขาดทุนรายวัน (${limit:g}) · "
               f"ราคาล่าสุด Bid {s.get('bid', 0):,.2f} / Ask {s.get('ask', 0):,.2f} · แจ้งเตือน: "
               f"Telegram {'✅' if alerts.get('telegram') else '❌'} · Healthchecks {'✅' if alerts.get('healthcheck') else '❌'}")
    what_bot_waits_for(s, now)
    st.subheader("ไม้ที่เปิดอยู่")
    if s.get("positions"):
        for p in s["positions"]:
            color = "green" if p["side"] == "BUY" else "red"
            st.markdown(f":{color}-badge[{p['side']}] **{p['volume']} lot** @ {p['price_open']:,.2f} → "
                        f"{p['price_current']:,.2f} · SL {p['sl']:,.2f} · "
                        f"**{p['profit']:+,.2f} {cur}**  \nเปิด {p['open_time']} · เหตุผล: {p['entry_reason'] or '-'}")
    else:
        st.markdown("ไม่มีไม้ที่เปิดอยู่")
    st.caption(f"เวลาเซิร์ฟเวอร์ MT5 {s.get('server_time')} · แท่งล่าสุดที่บอทประมวลผล {s.get('last_bar')}")


def load_trades() -> pd.DataFrame:
    tr = pd.DataFrame(list(db.trades.find({"bot_id": bot_id}, {"_id": 0, "bot_id": 0, "logged_at": 0})))
    if tr.empty:
        return tr
    for col in ("net_profit", "entry_price", "exit_price", "sl", "r_multiple", "mae_r", "mfe_r"):
        tr[col] = pd.to_numeric(tr[col], errors="coerce") if col in tr else np.nan
    return tr.sort_values("close_time").reset_index(drop=True)


def chart_tab(s: dict, now: datetime, trades: pd.DataFrame) -> None:
    cfg = s.get("config", {})
    st.subheader(f"ราคา {s.get('symbol', 'XAUUSD')} {cfg.get('timeframe')} (เวลาไทย)")
    bars = pd.DataFrame(list(db.bars.find({"bot_id": bot_id}, {"_id": 0}).sort("time", DESCENDING).limit(n_bars)))
    if bars.empty:
        st.caption("ยังไม่มีข้อมูลแท่งเทียน (รอบอทรันเวอร์ชันใหม่)")
    else:
        bars = bars.sort_values("time")
        bars["t"] = server_to_th(bars["time"]).values
        t0 = bars["t"].min()
        base = alt.Chart(bars).encode(x=alt.X("t:T", title=None, axis=alt.Axis(format="%d/%m %H:%M", labelAngle=0)))
        color = alt.condition("datum.o <= datum.c", alt.value(UP), alt.value(DOWN))
        tip = [alt.Tooltip("t:T", title="เวลาไทย", format="%d/%m %H:%M"), alt.Tooltip("o:Q", format=",.2f"),
               alt.Tooltip("h:Q", format=",.2f"), alt.Tooltip("l:Q", format=",.2f"), alt.Tooltip("c:Q", format=",.2f"),
               alt.Tooltip("ma_fast:Q", title=f"MA{cfg.get('fast')}", format=",.2f"),
               alt.Tooltip("ma_slow:Q", title=f"MA{cfg.get('slow')}", format=",.2f"), alt.Tooltip("adx:Q", format=".1f")]
        y = alt.Y("l:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format=",.0f"))
        layers = [
            base.mark_rule().encode(y=y, y2="h:Q", color=color, tooltip=tip),
            base.mark_bar(size=max(2, int(700 / len(bars)))).encode(y="o:Q", y2="c:Q", color=color, tooltip=tip),
            base.mark_line(strokeWidth=2, color=MA_FAST).encode(y="ma_fast:Q"),
            base.mark_line(strokeWidth=2, color=MA_SLOW).encode(y="ma_slow:Q"),
        ]
        marks = []
        if not trades.empty:
            vis = trades.assign(ot=server_to_th(trades["open_time"]).values, ct=server_to_th(trades["close_time"]).values)
            for r in vis.itertuples():
                if pd.notna(r.ot) and r.ot >= t0:
                    marks.append({"t": r.ot, "p": r.entry_price, "kind": f"เปิด {r.side}",
                                  "shape": "triangle-up" if r.side == "BUY" else "triangle-down", "txt": r.entry_reason})
                if pd.notna(r.ct) and r.ct >= t0:
                    marks.append({"t": r.ct, "p": r.exit_price, "kind": "ปิด", "shape": "circle",
                                  "txt": f"{r.exit_reason} ({r.net_profit:+.2f})"})
        for p in s.get("positions") or []:
            ot = server_to_th([p["open_time"]]).iloc[0]
            if pd.notna(ot) and ot >= t0:
                marks.append({"t": ot, "p": p["price_open"], "kind": f"เปิด {p['side']} (ถืออยู่)",
                              "shape": "triangle-up" if p["side"] == "BUY" else "triangle-down", "txt": p["entry_reason"]})
            sl_df = pd.DataFrame([{"sl": p["sl"], "label": f"SL {p['sl']:,.2f}"}])
            layers.append(alt.Chart(sl_df).mark_rule(color=DOWN, strokeDash=[6, 4]).encode(y="sl:Q"))
            layers.append(alt.Chart(sl_df).mark_text(align="left", dx=4, dy=-6, color=DOWN).encode(
                y="sl:Q", x=alt.value(0), text="label:N"))
        if marks:
            mk = pd.DataFrame(marks)
            layers.append(alt.Chart(mk).mark_point(size=140, filled=True, color=GOLD, stroke="#0e1117", strokeWidth=1.5)
                          .encode(x="t:T", y="p:Q", shape=alt.Shape("shape:N", scale=None),
                                  tooltip=[alt.Tooltip("t:T", title="เวลาไทย", format="%d/%m %H:%M"),
                                           alt.Tooltip("kind:N", title=""), alt.Tooltip("p:Q", title="ราคา", format=",.2f"),
                                           alt.Tooltip("txt:N", title="")]))
        st.altair_chart(alt.layer(*layers).properties(height=420), width="stretch")
        st.caption(f"เส้นฟ้า MA{cfg.get('fast')} · เส้นเทา MA{cfg.get('slow')} · ▲▼ จุดเข้า · ● จุดออก · เส้นประแดง SL ของไม้ที่ถืออยู่")

    since = now - timedelta(days=days)
    eq = pd.DataFrame(list(db.equity.find({"bot_id": bot_id, "time": {"$gte": since}},
                                          {"_id": 0, "time": 1, "balance": 1, "equity": 1}).sort("time", 1)))
    st.subheader("Balance / Equity")
    if len(eq) > 1:
        eq["time"] = pd.to_datetime(eq["time"], utc=True).dt.tz_convert(TH).dt.tz_localize(None)
        long = eq.melt("time", ["balance", "equity"], var_name="series", value_name="usd")
        chart = alt.Chart(long).mark_line(strokeWidth=2).encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("usd:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format=",.0f")),
            color=alt.Color("series:N", scale=alt.Scale(domain=["balance", "equity"], range=[MUTED, GOLD]),
                            legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[alt.Tooltip("time:T", title="เวลาไทย", format="%d/%m %H:%M"), alt.Tooltip("series:N", title=""),
                     alt.Tooltip("usd:Q", title="USD", format=",.2f")],
        ).properties(height=240)
        st.altair_chart(chart, width="stretch")
    else:
        st.caption("กราฟจะเริ่มแสดงหลังบอทรันไปสักพัก (บันทึกทุก 5 นาที)")


def news_tab(s: dict, now: datetime) -> None:
    cfg = s.get("config", {})
    upcoming = s.get("upcoming_news") or []
    if upcoming and "thai" in upcoming[0]:
        nxt = upcoming[0]
        note = f" · บอทงดเปิดไม้ใหม่ {nxt['block_th']} น." if cfg.get("news_filter") else ""
        st.info(f"**ข่าวถัดไป: {nxt['thai']}** — {nxt['time_th']} น. (อีก {until(nxt['time_utc'], now)})"
                + (f" · คาด {nxt['forecast']} / ครั้งก่อน {nxt['previous']}" if nxt["forecast"] else "") + note)
        st.dataframe(pd.DataFrame([{
            "เวลาไทย": u["time_th"], "อีก": until(u["time_utc"], now), "ข่าว": u["thai"], "ชื่อ MT5": u["event"],
            "คาดการณ์": u["forecast"], "ครั้งก่อน": u["previous"], "ผลต่อทอง": u["hint"],
            **({"งดเข้าไม้": u["block_th"]} if cfg.get("news_filter") else {}),
        } for u in upcoming]), hide_index=True, width="stretch")
    else:
        st.caption("ไม่มีข้อมูลปฏิทินข่าว")
    recent = s.get("recent_news") or []
    if recent:
        st.markdown("**ข่าวที่เพิ่งออก (3 วันล่าสุด)**")
        st.dataframe(pd.DataFrame([{
            "เวลาไทย": r["time_th"], "ข่าว": r["thai"], "จริง": r["actual"], "คาด": r["forecast"],
            "ครั้งก่อน": r["previous"], "ผลเทียบคาด": r["verdict"], "ตามหลักทั่วไป": r["bias"],
            "ทองขยับจริง 1 ชม.": (f"{r['gold_move_1h']:+,.2f} $" if r.get("gold_move_1h") is not None else ""),
        } for r in recent]), hide_index=True, width="stretch")
        st.caption("\"ตามหลักทั่วไป\" คือทิศที่ทองมักไปเมื่อตัวเลขต่างจากคาด (เช่น เงินเฟ้อสูงกว่าคาด → ดอลลาร์แข็ง → "
                   "ทองมักลง) แต่ตลาดอาจไม่ตามเสมอ — ดูช่อง \"ทองขยับจริง\" ประกอบ")


def live_vs_backtest(s: dict, trades: pd.DataFrame) -> None:
    st.subheader("ผลจริงเทียบ backtest")
    exp = s.get("expected") or {}
    sample = np.array(exp.get("r_sample") or [], dtype=float)
    live = trades["r_multiple"].dropna().to_numpy() if not trades.empty else np.array([])
    n = len(live)
    if not len(sample) or exp.get("avg_r") is None:
        st.caption("บอทนี้ยังไม่มีข้อมูล backtest ให้เทียบ (เช่น โปรไฟล์ทดลอง)")
        return
    c = st.columns(3)
    c[0].metric("R เฉลี่ยต่อไม้", f"{live.mean():+.2f}R" if n else "–", f"backtest {exp.get('avg_r') or 0:+.2f}R",
                delta_color="off", delta_arrow="off")
    c[1].metric("อัตราชนะ", f"{(live > 0).mean() * 100:.0f}%" if n else "–", f"backtest {exp.get('win_rate') or 0:.0f}%",
                delta_color="off", delta_arrow="off")
    c[2].metric("จำนวนไม้", f"{n}", f"backtest ~{exp.get('trades_per_year')} ไม้/ปี", delta_color="off", delta_arrow="off")
    horizon = max(n, 20)
    rng = np.random.default_rng(7)
    paths = rng.choice(sample, size=(3000, horizon)).cumsum(axis=1)
    band = pd.DataFrame({"trade": np.arange(1, horizon + 1), "p5": np.percentile(paths, 5, axis=0),
                         "p50": np.percentile(paths, 50, axis=0), "p95": np.percentile(paths, 95, axis=0)})
    layers = [alt.Chart(band).mark_area(opacity=0.25, color=MUTED).encode(
                  x=alt.X("trade:Q", title="ไม้ที่"), y=alt.Y("p5:Q", title="R สะสม"), y2="p95:Q"),
              alt.Chart(band).mark_line(strokeDash=[4, 4], color=MUTED).encode(x="trade:Q", y="p50:Q")]
    if n:
        actual = pd.DataFrame({"trade": np.arange(1, n + 1), "r": live.cumsum()})
        layers.append(alt.Chart(actual).mark_line(color=GOLD, strokeWidth=3, point=True).encode(
            x="trade:Q", y="r:Q", tooltip=[alt.Tooltip("trade:Q", title="ไม้ที่"), alt.Tooltip("r:Q", title="R สะสม", format="+.2f")]))
    st.altair_chart(alt.layer(*layers).properties(height=260), width="stretch")
    if n < 10:
        st.info(f"มีไม้จริง {n} ไม้ — ยังน้อยเกินสรุป (ต้อง ≥ 10 ไม้) แถบสีเทาคือช่วงปกติ 90% จากการสุ่มไม้ใน backtest "
                "เส้นทองคือผลจริง")
    else:
        lo, hi = band["p5"].iloc[n - 1], band["p95"].iloc[n - 1]
        total = live.sum()
        if total < lo:
            st.error(f"ผลจริง {total:+.1f}R ต่ำกว่าช่วงปกติของ backtest ({lo:+.1f} ถึง {hi:+.1f}R ที่ {n} ไม้) — "
                     "กลยุทธ์อาจไม่ได้ผลแล้ว หรือต้นทุนจริงสูงกว่าที่จำลอง ควรพิจารณาหยุดบอทและตรวจสอบ")
        elif total > hi:
            st.success(f"ผลจริง {total:+.1f}R ดีกว่าช่วงปกติ ({lo:+.1f} ถึง {hi:+.1f}R) — อาจเป็นช่วงโชคดี อย่าเพิ่มความเสี่ยง")
        else:
            st.success(f"ผลจริง {total:+.1f}R อยู่ในช่วงปกติของ backtest ({lo:+.1f} ถึง {hi:+.1f}R ที่ {n} ไม้)")
    st.caption(f"ที่มา: {exp.get('source', '')} · {len(sample)} ไม้ · R = กำไรสุทธิ ÷ ความเสี่ยงตอนเปิดไม้ (ระยะ SL)")


def performance_tab(s: dict, trades: pd.DataFrame) -> None:
    cur = s.get("currency", "USD")
    live_vs_backtest(s, trades)
    st.subheader("สถิติ")
    if trades.empty:
        st.caption("ยังไม่มีไม้ที่ปิด — สถิติจะขึ้นหลังบอทปิดไม้แรก")
        return
    p, r = trades["net_profit"], trades["r_multiple"].dropna()
    wins, losses = p[p > 0], p[p <= 0]
    eq = p.cumsum()
    c = st.columns(4)
    c[0].metric("ไม้ทั้งหมด", f"{len(p)}", f"ชนะ {len(wins) / len(p) * 100:.0f}%", delta_color="off", delta_arrow="off")
    c[1].metric(f"สุทธิ ({cur})", f"{p.sum():+,.2f}",
                f"PF {wins.sum() / -losses.sum():.2f}" if losses.sum() else "PF –", delta_color="off", delta_arrow="off")
    c[2].metric("ชนะ/แพ้ติดกันสูงสุด", f"{streak(list(p > 0), True)} / {streak(list(p > 0), False)}")
    c[3].metric(f"Drawdown สูงสุด ({cur})", f"{(eq.cummax().clip(lower=0) - eq).max():,.2f}")
    if len(r):
        c = st.columns(4)
        c[0].metric("Expectancy", f"{r.mean():+.2f}R")
        c[1].metric("ไม้ชนะเฉลี่ย", f"{r[r > 0].mean():+.2f}R" if (r > 0).any() else "–")
        c[2].metric("ไม้แพ้เฉลี่ย", f"{r[r <= 0].mean():+.2f}R" if (r <= 0).any() else "–")
        c[3].metric("ไม้ดีสุด / แย่สุด", f"{r.max():+.1f}R / {r.min():+.1f}R")

    left, right = st.columns(2)
    with left:
        st.markdown("**ผลรายเดือน (USD)**")
        mt = trades.assign(month=pd.to_datetime(trades["close_time"]).dt.to_period("M").astype(str)) \
            .groupby("month", as_index=False)["net_profit"].sum()
        mt["year"], mt["mon"] = mt["month"].str[:4], mt["month"].str[5:]
        lim = max(abs(mt["net_profit"]).max(), 1)
        heat = alt.Chart(mt).mark_rect(cornerRadius=3).encode(
            x=alt.X("mon:O", title="เดือน", axis=alt.Axis(labelAngle=0)), y=alt.Y("year:O", title=None),
            color=alt.Color("net_profit:Q", scale=alt.Scale(domain=[-lim, 0, lim], range=[DOWN, "#2a2f3a", UP]), legend=None),
            tooltip=[alt.Tooltip("month:N", title="เดือน"), alt.Tooltip("net_profit:Q", title=cur, format="+,.2f")])
        text = alt.Chart(mt).mark_text(fontSize=11, color="#e6e8ec").encode(x="mon:O", y="year:O", text=alt.Text("net_profit:Q", format="+,.0f"))
        st.altair_chart((heat + text).properties(height=60 * mt["year"].nunique() + 50), width="stretch")
    with right:
        st.markdown("**การกระจายของ R**")
        if len(r):
            hist = alt.Chart(pd.DataFrame({"r": r})).mark_bar(color=GOLD).encode(
                x=alt.X("r:Q", bin=alt.Bin(step=0.5), title="R ต่อไม้"), y=alt.Y("count():Q", title="จำนวนไม้"))
            st.altair_chart(hist.properties(height=180), width="stretch")
        else:
            st.caption("ไม้ที่ปิดยังไม่มีข้อมูล R")

    mm = trades.dropna(subset=["mae_r", "r_multiple"])
    if len(mm):
        st.markdown("**MAE / MFE — ไม้วิ่งสวนทางไปไกลสุดกี่ R ก่อนปิด**")
        mm = mm.assign(result=np.where(mm["r_multiple"] > 0, "ชนะ", "แพ้"))
        sc = alt.Chart(mm).mark_circle(size=90, opacity=0.85).encode(
            x=alt.X("mae_r:Q", title="MAE (R ที่วิ่งสวน)"), y=alt.Y("r_multiple:Q", title="ผลสุดท้าย (R)"),
            color=alt.Color("result:N", scale=alt.Scale(domain=["ชนะ", "แพ้"], range=[UP, DOWN]), legend=alt.Legend(title=None)),
            tooltip=["close_time:N", "side:N", alt.Tooltip("mae_r:Q", format=".2f"), alt.Tooltip("mfe_r:Q", format=".2f"),
                     alt.Tooltip("r_multiple:Q", format="+.2f")])
        st.altair_chart(sc.properties(height=240), width="stretch")
        win_mae = mm.loc[mm["r_multiple"] > 0, "mae_r"]
        if len(win_mae):
            st.caption(f"ไม้ชนะวิ่งสวนสูงสุด {win_mae.max():.2f}R (เฉลี่ย {win_mae.mean():.2f}R) — ถ้าไม้ชนะแทบไม่เคยวิ่งสวนเกิน "
                       "ระดับหนึ่ง แปลว่า SL อาจกว้างเกินไป (ต้องมีหลายสิบไม้ก่อนสรุป)")

    st.subheader("ไม้ที่ปิดแล้ว")
    show = trades.iloc[::-1].head(100)
    st.dataframe(show[[c for c in ("close_time", "side", "entry_price", "exit_price", "entry_reason", "exit_reason",
                                   "net_profit", "r_multiple", "mae_r", "mfe_r") if c in show]]
                 .rename(columns={"close_time": "ปิดเมื่อ", "side": "ฝั่ง", "entry_price": "เข้า", "exit_price": "ออก",
                                  "entry_reason": "เหตุผลเข้า", "exit_reason": "เหตุผลออก", "net_profit": "สุทธิ",
                                  "r_multiple": "R", "mae_r": "MAE (R)", "mfe_r": "MFE (R)"}),
                 hide_index=True, width="stretch", height=320)


def log_tab() -> None:
    icon = {"WARNING": "⚠️ ", "ERROR": "⛔ ", "CRITICAL": "⛔ "}
    ev = list(db.events.find({"bot_id": bot_id}).sort("time", DESCENDING).limit(100))
    if ev:
        st.dataframe(pd.DataFrame([{"เวลาไทย": th_time(e["time"]), "เหตุการณ์": icon.get(e["level"], "") + e["msg"]}
                                   for e in ev]), hide_index=True, width="stretch", height=600)
    else:
        st.caption("ยังไม่มี log")


def move_stats_box() -> None:
    """ค่าเฉลี่ยขึ้นลงต่อแท่ง (ดอลลาร์/จุด) ทุก timeframe + ภาวะ volatility/volume ตอนนี้เทียบปกติ (จาก research/move_stats.py — ไฟล์ static)"""
    f = Path(__file__).with_name("move_stats.json")
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return
    rows = []
    for tf, v in d.get("tf", {}).items():
        rows.append({"TF": tf, "ช่วงแท่งเฉลี่ย $": round(v["avg_range"], 2), "จุด (1$=100)": round(v["avg_range_pts"]), "เนื้อแท่ง $": round(v["avg_body"], 2),
                     "ATR14 $": round(v["atr14"], 2), "ตอนนี้เทียบปกติ (24 ชม.)": (f"{v['last24h']['ratio_range']:.2f}x · vol {v['last24h']['ratio_vol']:.2f}x" if v.get("last24h") else "-")})
    with st.expander("ค่าเฉลี่ยขึ้นลงต่อแท่งทุก timeframe + ภาวะตลาดตอนนี้", expanded=True):
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption(f"{d.get('point_note', '')} · ฐานเทียบ {d.get('baseline_days', '')} วันล่าสุด · volume ใช้เป็นตัวกรองความผันผวน ไม่ได้ทำนายทิศ")


def market_tab(now: datetime) -> None:
    move_stats_box()
    c = db.context.find_one({"_id": bot_id})
    if not c:
        st.caption("ยังไม่มีข้อมูลสภาพตลาด — บอทเวอร์ชันใหม่จะดึงทุก 20 นาทีหลังเริ่มรัน")
        return
    st.caption(f"อัปเดต {ago((now - c['updated_at']).total_seconds())} · ข้อมูลประกอบการดูเท่านั้น "
               "บอทไม่ได้ใช้ตัดสินใจเข้า/ออกไม้ (งานวิจัยพบว่าตัวกรองพวกนี้ไม่ได้ทำให้ผลดีขึ้น)")
    m = st.columns(5)
    vix, dxy, us10y, ry, cot = (c.get(k) or {} for k in ("vix", "dxy", "us10y", "real_yield", "cot"))
    if vix:
        m[0].metric(f"VIX ความกลัว · {vix.get('label', '')}", f"{vix['last']:.2f}", f"{vix['change']:+.2f}",
                    delta_color="inverse")
    if dxy:
        m[1].metric("ดัชนีดอลลาร์ (DXY)", f"{dxy['last']:.2f}", f"{dxy['change_pct']:+.2f}%", delta_color="inverse")
    if us10y:
        m[2].metric("พันธบัตร 10 ปี", f"{us10y['last']:.2f}%", f"{us10y['change']:+.3f}", delta_color="inverse")
    if ry:
        m[3].metric("Real yield 10 ปี", f"{ry['last']:.2f}%", f"{ry['change']:+.2f}" if ry.get("change") is not None else None,
                    delta_color="inverse")
    if cot:
        m[4].metric("กองทุนถือ long สุทธิ (COT)", f"{cot['net_pct']:.1f}% ของ OI", f"{cot['change_pct']:+.1f} จุด",
                    delta_color="off", delta_arrow="off")
    reads = []
    if vix:
        reads.append(f"**ความกลัว:** VIX {vix['last']:.1f} = {vix.get('label')} "
                     + ("— ตลาดกังวล นักลงทุนมักหนีเข้าทอง" if vix["last"] >= 20 else "— ไม่มีแรงหนีเข้าทองพิเศษ"))
    if dxy:
        reads.append(f"**ดอลลาร์:** {'แข็งขึ้น' if dxy['change'] > 0 else 'อ่อนลง'} {abs(dxy['change_pct']):.2f}% "
                     + ("→ มักกดดันทอง" if dxy["change"] > 0 else "→ มักหนุนทอง"))
    if us10y:
        reads.append(f"**ดอกเบี้ยพันธบัตร:** {'ขึ้น' if us10y['change'] > 0 else 'ลง'} "
                     + ("→ ถือทองมีต้นทุนค่าเสียโอกาสสูงขึ้น มักกดดันทอง" if us10y["change"] > 0 else "→ มักหนุนทอง"))
    if cot:
        crowd = "สูงกว่าปกติมาก (ถือแน่น)" if cot["rank_3y"] >= 80 else "ต่ำกว่าปกติมาก" if cot["rank_3y"] <= 20 else "ระดับปกติ"
        reads.append(f"**กองทุน:** ถือ long สุทธิ {cot['net_pct']:.1f}% ของสัญญาทั้งหมด — {crowd} "
                     f"(สูงกว่า {cot['rank_3y']}% ของ 3 ปีที่ผ่านมา, ข้อมูลวันที่ {cot['date']})")
    if reads:
        with st.container(border=True):
            st.markdown("**อ่านตลาดแบบเร็ว** (ทิศที่ \"มัก\" เกิด ไม่ได้เกิดทุกครั้ง)  \n" + "  \n".join(f"• {r}" for r in reads))
    st.subheader("พาดหัวข่าวทองล่าสุด")
    heads = c.get("headlines") or []
    if heads:
        for h in heads:
            st.markdown(f"**{h['source']}** · {ago((now - h['time_utc']).total_seconds())} — [{h['title']}]({h['link']})")
    else:
        st.caption("ดึงพาดหัวข่าวไม่ได้รอบนี้")
    if c.get("errors"):
        st.caption("แหล่งที่ดึงไม่ได้รอบล่าสุด: " + ", ".join(c["errors"]))


@st.cache_data(ttl=60, show_spinner=False)
def load_all_trades() -> list[dict]:
    return list(db.trades.find({}, {"_id": 0, "bot_id": 1, "close_time": 1, "open_time": 1, "net_profit": 1, "r_multiple": 1, "source": 1, "side": 1,
                                    "entry_price": 1, "exit_price": 1, "exit_reason": 1, "entry_reason": 1, "lot": 1, "sl": 1, "tier": 1, "tp_usd": 1,
                                    "spread_pts": 1, "slippage_pts": 1, "ticket": 1, "features": 1}))


@st.cache_data(ttl=30, show_spinner=False)
def load_m5_bars(bot_ids_m5: tuple) -> pd.DataFrame:
    """แท่ง M5 ที่บอทฝึกส่งขึ้น Mongo (รวมจากหลายบอท ตัดซ้ำตามเวลา) — ใช้ทำกราฟรวมและวิเคราะห์ 'ปิดมือเร็วไปไหม'"""
    docs = list(db.bars.find({"bot_id": {"$in": list(bot_ids_m5)}}, {"_id": 0, "time": 1, "o": 1, "h": 1, "l": 1, "c": 1}))
    if not docs:
        return pd.DataFrame()
    b = pd.DataFrame(docs).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    b["tsrv"] = pd.to_datetime(b["time"], errors="coerce")
    return b.dropna(subset=["tsrv"])


def trade_frame(real: bool = False) -> pd.DataFrame:
    """ไม้ที่ปิดแล้วของบอท — real=False ไม่รวมบัญชีจริง (กันโผล่ในหน้าสาธารณะ), real=True เฉพาะบัญชีจริง (เรียกหลังใส่ PIN เท่านั้น)"""
    docs = [x for x in load_all_trades() if (x.get("bot_id") in real_ids) == real and x.get("bot_id") in server_by_bot]
    if not docs:
        return pd.DataFrame()
    df = pd.DataFrame(docs)
    for col in ("source", "tier", "tp_usd", "spread_pts", "slippage_pts", "open_time", "exit_reason", "lot", "entry_price", "exit_price", "sl", "side"):
        if col not in df:
            df[col] = np.nan
    df["บอท"] = df["bot_id"].map(lambda i: bot_labels.get(i, i))
    df["tcl"] = pd.to_datetime(df["close_time"], errors="coerce")  # เวลาเซิร์ฟเวอร์ (ใช้เทียบกับแท่งราคา)
    df["top"] = pd.to_datetime(df["open_time"], errors="coerce")
    df["โบรกเกอร์"] = df["bot_id"].map(lambda i: "Exness" if server_by_bot.get(i, "").lower().startswith("exness") else "MetaQuotes")
    ex = (df["โบรกเกอร์"] == "Exness").to_numpy()
    df["ปิดเมื่อ"] = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    df["เปิดเมื่อ"] = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    for mask, utc in ((~ex, False), (ex, True)):
        if mask.any():
            df.loc[mask, "ปิดเมื่อ"] = server_to_th(df.loc[mask, "close_time"], utc=utc).values
            df.loc[mask, "เปิดเมื่อ"] = server_to_th(df.loc[mask, "open_time"], utc=utc).values
    df["กำไร $"] = pd.to_numeric(df["net_profit"], errors="coerce")
    df["R"] = pd.to_numeric(df["r_multiple"], errors="coerce")
    for col in ("entry_price", "exit_price", "lot", "sl", "tp_usd", "spread_pts", "slippage_pts"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["tier"] = df["tier"].fillna("").astype(str).replace("nan", "")
    df = df.dropna(subset=["ปิดเมื่อ", "กำไร $"]).sort_values("ปิดเมื่อ").reset_index(drop=True)
    df["ถือ (นาที)"] = ((df["ปิดเมื่อ"] - df["เปิดเมื่อ"]).dt.total_seconds() / 60).round()
    df["ที่มา"] = df["source"].fillna("signal")
    df["ปิดโดย"] = np.where(df["exit_reason"].astype(str).str.contains("มือ|สั่งปิด|dashboard", regex=True), "ปิดมือ", "บอท (SL/TP/สัญญาณ)")
    df["win"] = df["กำไร $"] > 0
    # ตัวแปร indicator ตอนเข้าไม้ (JSON ใน features) → คอลัมน์สำคัญ ๆ
    import json as _json
    def _feat(x, k):
        try:
            return _json.loads(x).get(k) if isinstance(x, str) and x else np.nan
        except Exception:
            return np.nan
    if "features" in df:
        for k in ("rsi14", "adx", "bb_pctb", "range_pos_100_side", "macd_hist_atr", "atr_rank200", "spread_atr", "hour_utc"):
            df[f"f_{k}"] = df["features"].map(lambda x, k=k: _feat(x, k))
    prof_by_bot = {d["_id"]: str(d.get("profile") or "main") for d in _st}
    df["โปรไฟล์"] = df["bot_id"].map(lambda i: prof_by_bot.get(i, "main")).str.replace("^ex-", "", regex=True)
    pt = np.where(df["โบรกเกอร์"] == "Exness", 0.001, 0.01)
    df["spread $"] = (df["spread_pts"] * pt).round(2)
    df["slip $"] = (df["slippage_pts"] * pt).round(2)
    return df


def status_goal_section() -> None:
    now_utc = datetime.now(timezone.utc)
    fleet = []
    for d in db.status.find({}, {"indicators": 0, "upcoming_news": 0, "recent_news": 0, "params_info": 0, "expected": 0}):
        up = d.get("updated_at")
        if up is not None and up.tzinfo is None:
            up = up.replace(tzinfo=timezone.utc)
        age = (now_utc - up).total_seconds() if up else None
        online = bool(d.get("running")) and age is not None and age < 60
        al = d.get("alerts") or {}
        fleet.append({"บอท": bot_labels.get(d["_id"], d["_id"]), "สถานะ": "🟢 ออนไลน์" if online else "🔴 ไม่ตอบ",
                      "อัปเดตล่าสุด": ago(age) if age is not None else "-", "ไม้เปิดอยู่": len(d.get("positions") or []),
                      "ไม้วันนี้": d.get("entries_today"), "กำไรวันนี้ $": round(float(d.get("day_pnl") or 0), 2),
                      "หยุดเข้าไม้": "⏸" if d.get("paused") else "", "Telegram": "✅" if al.get("telegram") else "❌",
                      "Healthchecks": "✅" if al.get("healthcheck") else "❌", "แท่งถัดไป": d.get("next_bar_close")})
    if fleet:
        st.markdown("**สถานะบอททุกตัวตอนนี้**")
        st.dataframe(pd.DataFrame(fleet), width="stretch", hide_index=True)
    goal = db.team.find_one({"_id": "goal"})
    if not goal:
        st.info("ยังไม่มีข้อมูลเป้าของทีม (ตัวคุมทีมยังไม่ส่งขึ้นมา)")
        return
    tgt, pnl = float(goal.get("target") or 1000), float(goal.get("team_pnl") or 0)
    up_g = goal.get("updated_at")
    if up_g is not None:
        up_g = up_g if up_g.tzinfo else up_g.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - up_g).total_seconds() > 120:
            st.warning(f"ตัวคุมทีมไม่อัปเดตมา {ago((datetime.now(timezone.utc) - up_g).total_seconds())} — ตัวเลขทีมด้านล่างอาจไม่ใช่ปัจจุบัน")
    status = ("✅ ถึงเป้าแล้ว — ทีมหยุดเปิดไม้ใหม่" if goal.get("target_reached") else "🛑 ทีมหยุดเพราะถึงเส้นหยุด" if goal.get("halt_all") else "กำลังเดินหน้า")
    st.markdown(f"**เป้ากำไรรวมของทุกบอท: +${tgt:,.0f}** · ตอนนี้ **{pnl:+,.2f}** (ปิดแล้ว {float(goal.get('realized') or 0):+,.2f} · ลอย "
                f"{float(goal.get('floating') or 0):+,.2f}) · {status} · {'เส้นกันคืนกำไรทีม +' if goal.get('stage') == 2 else 'ลิมิตขาดทุนทีม '}${float(goal.get('hard_stop') or 0):,.0f}")
    st.progress(min(max(pnl / tgt, 0.0), 1.0), text=f"{pnl / tgt:.0%} ของเป้า")
    if goal.get("second_target"):
        note = goal.get("stage2_note") or ""
        st.caption(f"ขั้นที่ {goal.get('stage', 1)}/2 · ถ้าถึง +${float(goal.get('first_target') or 1000):,.0f} แล้วเทรนด์ H1/H4/D1 ชัดและเวลาพอ จะไปต่อถึง "
                   f"+${float(goal['second_target']):,.0f} (ถ้ากำไรลดเหลือครึ่งที่ล็อกไว้ หยุดทันที) " + (f"· {note}" if note else ""))
    pb = goal.get("per_bot") or {}
    if pb:
        st.dataframe(pd.DataFrame([{"บอท": k, "ปิดแล้ว $": v.get("realized"), "ลอย $": v.get("floating"),
                                     "รวม $": round((v.get("realized") or 0) + (v.get("floating") or 0), 2)} for k, v in pb.items()]
                                   ).sort_values("รวม $", ascending=False), width="stretch", hide_index=True)
    st.caption("นับตั้งแต่เริ่มเป้า (tools/team_monitor.py) · นับไม้ที่ปิดมือด้วย · ถึงเป้า → ปิดไม้ของทุกบอท + หยุดเปิดไม้ใหม่ · ไม่การันตีว่าจะถึงเป้า")


def compare_section(df: pd.DataFrame) -> None:
    """ตัวไหนทำกำไรได้ดีที่สุด รายวัน/รายสัปดาห์/รายเดือน (เวลาไทย, ไม้ที่ปิดแล้วเท่านั้น)"""
    if df.empty:
        st.info("ยังไม่มีไม้ที่ปิดแล้วจากบอทใด")
        return
    df = df.copy()
    df["net"] = df["กำไร $"]
    df["t"] = df["ปิดเมื่อ"]
    df["bot"] = df["บอท"]
    period = st.radio("ช่วงเวลา", ["รายวัน", "รายสัปดาห์", "รายเดือน"], horizontal=True, key="cmp_period")
    if period == "รายวัน":
        df["p"] = df["t"].dt.normalize(); fmt = lambda x: f"{x:%d/%m}"
    elif period == "รายสัปดาห์":
        df["p"] = df["t"].dt.to_period("W-SUN").dt.start_time; fmt = lambda x: f"สัปดาห์ {x:%d/%m}"
    else:
        df["p"] = df["t"].dt.to_period("M").dt.start_time; fmt = lambda x: f"{x:%m/%Y}"
    df["plabel"] = df["p"].map(fmt)
    total = df.groupby("bot")["net"].sum().sort_values(ascending=False)
    latest = df["p"].max()
    cur = df[df["p"] == latest].groupby("bot")["net"].sum().sort_values(ascending=False)
    lab = period[3:] if period != "รายวัน" else ("วันนี้" if latest.date() == datetime.now(TH).date() else "วันล่าสุด")
    c = st.columns(3)
    c[0].metric(f"ดีที่สุด{lab}({fmt(latest)})", cur.index[0], f"{cur.iloc[0]:+,.2f} USD", delta_color="off", delta_arrow="off")
    c[1].metric("ดีที่สุดรวมทั้งหมด", total.index[0], f"{total.iloc[0]:+,.2f} USD", delta_color="off", delta_arrow="off")
    c[2].metric("แย่ที่สุดรวมทั้งหมด", total.index[-1], f"{total.iloc[-1]:+,.2f} USD", delta_color="off", delta_arrow="off")

    def stats(g: pd.DataFrame) -> pd.Series:
        gw, gl = g.loc[g.net > 0, "net"].sum(), -g.loc[g.net < 0, "net"].sum()
        daily = g.groupby(g["t"].dt.normalize())["net"].sum()
        return pd.Series({"ไม้": len(g), "ชนะ %": round(g.win.mean() * 100), "กำไรสุทธิ $": round(g.net.sum(), 2), "เฉลี่ย $/ไม้": round(g.net.mean(), 2),
                          "R เฉลี่ย": round(g.R.mean(), 2) if g.R.notna().any() else np.nan, "PF": round(gw / gl, 2) if gl else np.nan,
                          "วันดีสุด $": round(daily.max(), 2), "วันแย่สุด $": round(daily.min(), 2)})
    st.markdown("**อันดับรวมทุกไม้ที่ปิดแล้ว**")
    st.dataframe(pd.DataFrame({b: stats(g) for b, g in df.groupby("bot")}).T.sort_values("กำไรสุทธิ $", ascending=False), width="stretch")
    per = df.groupby(["p", "plabel", "bot"], as_index=False).agg(net=("net", "sum"), trades=("net", "size"), win=("win", "mean")).sort_values("p")
    order = list(per.drop_duplicates("plabel")["plabel"])
    st.markdown(f"**กำไรสุทธิ ({period}) แยกตามบอท**")
    st.altair_chart(alt.Chart(per).mark_bar().encode(
        x=alt.X("plabel:N", sort=order, title=None), xOffset="bot:N", y=alt.Y("net:Q", title="กำไรสุทธิ (USD)"), color=alt.Color("bot:N", title="บอท"),
        tooltip=["plabel", "bot", alt.Tooltip("net:Q", format="+,.2f"), "trades", alt.Tooltip("win:Q", format=".0%")]), width="stretch")
    pivot = per.pivot_table(index=["p", "plabel"], columns="bot", values="net", aggfunc="sum").sort_index(ascending=False)
    pivot["🏆 ดีที่สุด"] = pivot.idxmax(axis=1)
    pivot.index = pivot.index.get_level_values("plabel")
    st.markdown(f"**ตารางกำไร ($) {period} — ผู้ชนะแต่ละช่วงอยู่คอลัมน์ท้าย**")
    st.dataframe(pivot.round(2), width="stretch")
    cum = df.sort_values("t").assign(cum=lambda x: x.groupby("bot")["net"].cumsum())
    st.markdown("**กำไรสะสมของแต่ละบอท (เรียงตามเวลาปิดไม้)**")
    st.altair_chart(alt.Chart(cum).mark_line().encode(x=alt.X("t:T", title="เวลาไทย"), y=alt.Y("cum:Q", title="กำไรสะสม (USD)"), color=alt.Color("bot:N", title="บอท"),
                                                      tooltip=["bot", "t:T", alt.Tooltip("cum:Q", format="+,.2f")]), width="stretch")
    st.caption("ขนาดไม้ของบอทแต่ละตัวต่างกัน (บอท H4 เสี่ยง 0.5%/ไม้, บอทฝึก 0.05%/ไม้) — เทียบ R ประกอบ อย่าเทียบ $ อย่างเดียว")


def journal_section(df: pd.DataFrame) -> None:
    """ไม้เปิดอยู่ทุกบอท · กำไรสะสมรวม · สมุดบันทึกไม้ (กรองได้) · ปิดมือ vs บอท"""
    rows = []
    for d in db.status.find({"positions.0": {"$exists": True}}, {"indicators": 0, "upcoming_news": 0, "recent_news": 0, "params_info": 0, "expected": 0}):
        for p in d.get("positions") or []:
            rows.append({"บอท": bot_labels.get(d["_id"], d["_id"]), "ด้าน": p.get("side"), "lot": p.get("volume"), "ราคาเข้า": p.get("price_open"),
                         "ราคาตอนนี้": p.get("price_current"), "SL": p.get("sl"), "TP": p.get("tp") or None, "กำไรลอย $": p.get("profit"),
                         "เปิดเมื่อ (เซิร์ฟเวอร์)": p.get("open_time"), "ticket": p.get("ticket")})
    st.markdown("**ไม้ที่เปิดอยู่ทุกบอท**")
    if rows:
        op = pd.DataFrame(rows)
        c = st.columns(3)
        c[0].metric("ไม้เปิดอยู่ทั้งหมด", len(op)); c[1].metric("กำไรลอยรวม", f"{op['กำไรลอย $'].sum():+,.2f} USD")
        c[2].metric("BUY / SELL", f"{(op['ด้าน'] == 'BUY').sum()} / {(op['ด้าน'] == 'SELL').sum()}")
        st.dataframe(op, width="stretch", hide_index=True)
    else:
        st.info("ตอนนี้ไม่มีไม้เปิดอยู่ในบอทตัวไหนเลย")
    if df.empty:
        return
    goal = db.team.find_one({"_id": "goal"})
    cum = df.assign(สะสม=df.groupby("โบรกเกอร์")["กำไร $"].cumsum())
    st.markdown("**กำไรสะสมรวมทุกบอท แยกตามโบรกเกอร์ (บัญชีคนละขนาด จึงไม่รวมกัน)**")
    layers = [alt.Chart(cum).mark_line(point=True).encode(x=alt.X("ปิดเมื่อ:T", title="เวลาไทย"), y=alt.Y("สะสม:Q", title="กำไรสะสม (USD)"),
              color=alt.Color("โบรกเกอร์:N"), tooltip=["โบรกเกอร์", "บอท", "ปิดเมื่อ:T", alt.Tooltip("กำไร $:Q", format="+,.2f"), alt.Tooltip("สะสม:Q", format="+,.2f")])]
    if goal and goal.get("target"):
        layers.append(alt.Chart(pd.DataFrame({"y": [float(goal["target"])]})).mark_rule(strokeDash=[6, 4], color="#d4a017").encode(y="y:Q"))
    st.altair_chart(alt.layer(*layers), width="stretch")
    st.markdown("**สมุดบันทึกไม้**")
    f1, f2, f3 = st.columns(3)
    bots_sel = f1.multiselect("บอท", sorted(df["บอท"].unique()), default=[], key="jr_bots", placeholder="ทุกบอท")
    src_sel = f2.multiselect("ที่มาของไม้", sorted(df["ที่มา"].unique()), default=[], key="jr_src", placeholder="ทุกที่มา")
    side_sel = f3.multiselect("ด้าน", ["BUY", "SELL"], default=[], key="jr_side", placeholder="ทั้งสองด้าน")
    v = df
    if bots_sel: v = v[v["บอท"].isin(bots_sel)]
    if src_sel: v = v[v["ที่มา"].isin(src_sel)]
    if side_sel: v = v[v["side"].isin(side_sel)]
    if v.empty:
        st.info("ไม่มีไม้ตรงตัวกรอง")
        return
    gw, gl = v.loc[v["กำไร $"] > 0, "กำไร $"].sum(), -v.loc[v["กำไร $"] < 0, "กำไร $"].sum()
    m = st.columns(5)
    m[0].metric("จำนวนไม้", len(v)); m[1].metric("กำไรสุทธิ", f"{v['กำไร $'].sum():+,.2f}"); m[2].metric("ชนะ", f"{(v['กำไร $'] > 0).mean():.0%}")
    m[3].metric("เฉลี่ย/ไม้", f"{v['กำไร $'].mean():+.2f}"); m[4].metric("Profit factor", f"{gw / gl:.2f}" if gl else "–")
    cols = ["ปิดเมื่อ", "บอท", "side", "lot", "entry_price", "exit_price", "กำไร $", "R", "ถือ (นาที)", "ปิดโดย", "exit_reason", "ที่มา", "tier", "tp_usd", "spread $", "slip $", "โบรกเกอร์",
            "f_rsi14", "f_adx", "f_bb_pctb", "f_range_pos_100_side", "f_macd_hist_atr", "f_atr_rank200", "f_spread_atr", "f_hour_utc"]
    st.dataframe(v.sort_values("ปิดเมื่อ", ascending=False)[[c for c in cols if c in v]].rename(columns={"side": "ด้าน", "entry_price": "ราคาเข้า", "exit_price": "ราคาออก",
                 "exit_reason": "เหตุผลปิด", "f_rsi14": "RSI", "f_adx": "ADX", "f_bb_pctb": "BB %B", "f_range_pos_100_side": "ตำแหน่งในช่วง100(ตามทิศ)",
                 "f_macd_hist_atr": "MACD hist/ATR", "f_atr_rank200": "ATR rank", "f_spread_atr": "spread/ATR", "f_hour_utc": "ชั่วโมง UTC"}), width="stretch", hide_index=True)
    st.caption("คอลัมน์ RSI→ชั่วโมง UTC คือตัวแปร indicator ตอนเข้าไม้ (เริ่มบันทึกตั้งแต่เวอร์ชันนี้) · วิเคราะห์ความสัมพันธ์กับกำไรด้วย tools/feature_report.py")
    st.markdown("**ปิดมือ vs บอทปิดเอง (ตามตัวกรองด้านบน)**")
    comp = v.groupby("ปิดโดย").agg(ไม้=("กำไร $", "size"), กำไรสุทธิ=("กำไร $", "sum"), เฉลี่ยต่อไม้=("กำไร $", "mean"), ชนะ=("กำไร $", lambda x: (x > 0).mean() * 100),
                                   ถือเฉลี่ย_นาที=("ถือ (นาที)", "mean")).round(2)
    st.dataframe(comp, width="stretch")


def m5_source_ids() -> tuple:
    # แท่งราคา/เวลาเซิร์ฟเวอร์ของ MetaQuotes เท่านั้น (Exness คนละโซนเวลา/ราคา จึงไม่ปนในกราฟรวมและการวิเคราะห์ปิดมือ)
    return tuple(d["_id"] for d in db.status.find({"config.timeframe": "M5", "server": {"$not": {"$regex": "^Exness", "$options": "i"}}}, {"_id": 1}))


def all_chart_section(df: pd.DataFrame) -> None:
    """กราฟราคา M5 พร้อมจุดเข้า-ออกของทุกบอท (สีตามบอท) — เห็นภาพเดียวว่าใครเข้าตรงไหน"""
    ids = m5_source_ids()
    bars = load_m5_bars(ids) if ids else pd.DataFrame()
    if bars.empty:
        st.info("ยังไม่มีข้อมูลแท่ง M5 (บอท M5 ต้องรันก่อน)")
        return
    n = st.slider("จำนวนแท่ง M5 ที่แสดง", 120, 1500, 400, step=40, key="all_chart_n")
    b = bars.tail(n).copy()
    b["t"] = server_to_th(b["time"]).values
    t0 = b["tsrv"].min()
    base = alt.Chart(b).encode(x=alt.X("t:T", title=None, axis=alt.Axis(format="%d/%m %H:%M", labelAngle=0)))
    color = alt.condition("datum.o <= datum.c", alt.value(UP), alt.value(DOWN))
    y = alt.Y("l:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format=",.0f"))
    layers = [base.mark_rule().encode(y=y, y2="h:Q", color=color), base.mark_bar(size=max(2, int(900 / len(b)))).encode(y="o:Q", y2="c:Q", color=color)]
    if not df.empty:
        v = df[(df["โบรกเกอร์"] == "MetaQuotes") & ((df["top"] >= t0) | (df["tcl"] >= t0))].dropna(subset=["entry_price", "exit_price"])
        if not v.empty:
            legs = pd.concat([
                pd.DataFrame({"id": v.index, "บอท": v["บอท"], "t": v["เปิดเมื่อ"], "p": v["entry_price"], "kind": "เข้า " + v["side"].astype(str), "กำไร": v["กำไร $"]}),
                pd.DataFrame({"id": v.index, "บอท": v["บอท"], "t": v["ปิดเมื่อ"], "p": v["exit_price"], "kind": "ออก", "กำไร": v["กำไร $"]})])
            layers.append(alt.Chart(legs).mark_line(opacity=0.7, strokeWidth=2).encode(x="t:T", y="p:Q", detail="id:N", color=alt.Color("บอท:N")))
            ent, ext = legs[legs["kind"] != "ออก"], legs[legs["kind"] == "ออก"]
            tip = ["บอท", "kind", alt.Tooltip("t:T", format="%d/%m %H:%M"), alt.Tooltip("p:Q", format=",.2f"), alt.Tooltip("กำไร:Q", format="+,.2f")]
            layers.append(alt.Chart(ent).mark_point(size=110, filled=True, stroke="#0e1117", strokeWidth=1).encode(
                x="t:T", y="p:Q", color=alt.Color("บอท:N"), shape=alt.Shape("kind:N", scale=alt.Scale(domain=["เข้า BUY", "เข้า SELL"], range=["triangle-up", "triangle-down"])), tooltip=tip))
            layers.append(alt.Chart(ext).mark_point(size=70, filled=False, strokeWidth=2).encode(x="t:T", y="p:Q", color=alt.Color("บอท:N"), tooltip=tip))
    st.altair_chart(alt.layer(*layers).properties(height=460).resolve_scale(color="independent"), width="stretch")
    st.caption("แท่ง M5 (เวลาไทย) · ▲▼ = จุดเข้า BUY/SELL · ○ = จุดออก · เส้นสีเชื่อมจุดเข้า→ออกของแต่ละไม้ แยกสีตามบอท")


def early_close_section(df: pd.DataFrame) -> None:
    """ไม้ที่ปิดด้วยมือ: ถ้าปล่อยไว้ต่ออีก 30 นาที/1/2 ชั่วโมง (หรือชน SL เดิมก่อน) จะได้ต่างจากที่ปิดเท่าไร"""
    ids = m5_source_ids()
    bars = load_m5_bars(ids) if ids else pd.DataFrame()
    if df.empty or bars.empty:
        st.info("ยังไม่มีข้อมูลพอ (ต้องมีไม้ปิดมือ และแท่ง M5 หลังเวลาปิด)")
        return
    man = df[(df["โบรกเกอร์"] == "MetaQuotes") & (df["ปิดโดย"] == "ปิดมือ") & df["tcl"].notna() & df["entry_price"].notna() & df["exit_price"].notna() & df["lot"].notna()]
    if man.empty:
        st.info("ยังไม่มีไม้ที่ปิดด้วยมือ")
        return
    tt = bars["tsrv"].to_numpy()
    rows = []
    for r in man.itertuples():
        side = 1 if r.side == "BUY" else -1
        after = bars[bars["tsrv"] >= r.tcl]
        if after.empty:
            continue
        mult = side * float(r.lot) * 100  # $ ต่อการขยับราคา 1 ดอลลาร์
        realized_px = (r.exit_price - r.entry_price) * mult
        row = {"บอท": r.บอท, "ด้าน": r.side, "ปิดเมื่อ": r.ปิดเมื่อ, "ปิดมือได้ $": round(float(df.loc[r.Index, "กำไร $"]), 2)}
        ext = after.head(24)  # 2 ชั่วโมงถัดไป
        fav = ((ext["h"].max() if side == 1 else ext["l"].min()) - r.exit_price) * side
        row["ราคาไปต่อสูงสุดใน 2 ชม. ($/oz)"] = round(fav, 2)
        for lab, k in (("+30 นาที", 6), ("+1 ชม.", 12), ("+2 ชม.", 24)):
            seg = after.head(k)
            if len(seg) < k:
                row[f"ถ้าถือ {lab} ต่างจากปิด $"] = None
                continue
            hit_sl = False
            if pd.notna(r.sl):
                hit_sl = bool(((seg["l"] <= r.sl).any() if side == 1 else (seg["h"] >= r.sl).any()))
            px = r.sl if hit_sl else seg["c"].iloc[-1]
            row[f"ถ้าถือ {lab} ต่างจากปิด $"] = round(((px - r.entry_price) * mult) - realized_px, 2)
            if lab == "+1 ชม.":
                row["ชน SL ก่อน (1 ชม.)"] = "ใช่" if hit_sl else ""
        rows.append(row)
    if not rows:
        st.info("ยังไม่มีแท่งราคาหลังเวลาปิดของไม้ปิดมือ")
        return
    out = pd.DataFrame(rows)
    st.dataframe(out, width="stretch", hide_index=True)
    sums = {c: out[c].dropna().sum() for c in out.columns if c.startswith("ถ้าถือ")}
    cnt = {c: out[c].notna().sum() for c in sums}
    st.markdown("**สรุป (ผลรวมส่วนต่างเทียบกับที่คุณปิดมือ — บวก = ถือต่อได้เพิ่ม, ลบ = ปิดมือดีแล้ว)**")
    sc = st.columns(len(sums) or 1)
    for col, (name, tot) in zip(sc, sums.items()):
        col.metric(name.replace("ต่างจากปิด $", "").strip(), f"{tot:+,.2f} USD", f"จาก {cnt[name]} ไม้", delta_color="off", delta_arrow="off")
    st.caption("คำนวณจากแท่ง M5 หลังเวลาปิด: ถ้าราคาแตะ SL เดิมก่อนครบเวลา นับว่าโดน SL · ใช้ราคาปิดแท่งสุดท้ายของช่วง ไม่รวม slippage/spread ขาออก · "
               "ตัวอย่างน้อยมาก ใช้ดูแนวโน้มเท่านั้น ไม่ใช่ข้อสรุป")


def broker_compare_section(df: pd.DataFrame) -> None:
    """กลยุทธ์เดียวกันบนสองโบรกเกอร์: ต้นทุนจริง (spread/slippage เป็นดอลลาร์) และผลต่อไม้ — ใช้ตัดสินว่าโบรกเกอร์ไหนเหมาะกับการเทรดจริง"""
    ex_ids = [i for i, sv in server_by_bot.items() if sv.lower().startswith("exness")]
    if df.empty or not ex_ids:
        st.info("ยังไม่มีข้อมูลจากบัญชี Exness")
        return
    d = df.copy()
    g = d.groupby(["โปรไฟล์", "โบรกเกอร์"]).agg(ไม้=("กำไร $", "size"), กำไรสุทธิ=("กำไร $", "sum"), เฉลี่ยต่อไม้=("กำไร $", "mean"),
                                               ชนะ=("กำไร $", lambda x: (x > 0).mean() * 100), spread=("spread $", "mean"), slip=("slip $", "mean")).round(2)
    st.markdown("**ผลและต้นทุนจริงต่อกลยุทธ์ แยกตามโบรกเกอร์** (ขนาดบัญชีต่างกัน $100,000 vs $10,000 ดูเฉลี่ยต่อไม้และต้นทุน อย่าเทียบ $ รวม)")
    st.dataframe(g.rename(columns={"spread": "spread เฉลี่ยตอนเข้า $", "slip": "slippage เฉลี่ย $"}), width="stretch")
    c = d.groupby("โบรกเกอร์").agg(ไม้=("กำไร $", "size"), spread=("spread $", "mean"), slip=("slip $", "mean")).round(3)
    cols = st.columns(max(len(c), 1))
    for col, (b, r) in zip(cols, c.iterrows()):
        col.metric(f"{b} · ต้นทุนเข้าเฉลี่ย", f"spread ${r['spread']:.2f}", f"slip ${r['slip']:+.3f} · {int(r['ไม้'])} ไม้", delta_color="off", delta_arrow="off")
    st.caption("สัญญาณของสองโบรกเกอร์ไม่เหมือนกันเป๊ะ (ราคา/เวลาเซิร์ฟเวอร์/spread ต่าง) ใช้เทียบ 'ต้นทุนและความเป็นไปได้' ไม่ใช่เทียบไม้ต่อไม้ · ต้องมีไม้เยอะพอก่อนสรุป")


def _daily_data() -> dict:
    """ไฟล์ static จาก tools/daily_refresh.py (งานรายวัน 06:00) — ข่าวข้างหน้า, ผลวิเคราะห์, สถิติตัวกรอง volume (ไม่มีข้อมูลบัญชี)"""
    try:
        return json.loads(Path(__file__).with_name("daily_report.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def news_blocking_table() -> None:
    d = _daily_data()
    rows = d.get("news") or []
    st.markdown("**ข่าว USD ใน 36 ชม. ข้างหน้า และบอทบล็อกไหม**")
    if not rows:
        st.caption("ยังไม่มีรายงานประจำวัน (งานรายวัน 06:00 ยังไม่รัน)")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(f"อัปเดต {d.get('generated', '-')} (เวลาเครื่อง) · บอทบล็อกก่อน/หลังข่าว 30 นาทีเฉพาะ m15b/m30b และเฉพาะข่าวความสำคัญ ≥3 · บอท M5 ไม่กรองข่าว")


def daily_report_box() -> None:
    d = _daily_data()
    news_blocking_table()
    an = d.get("analysis") or {}
    for title, text in an.items():
        with st.expander(title, expanded=False):
            st.code(text, language=None)
    if an:
        st.caption("ผลวิเคราะห์เทียบข้อมูลราคาย้อนหลัง (SL 7.5 / TP 15) อัปเดตทุกเช้า — เป็นตัวชี้ ไม่ใช่การรับประกันกำไร")


def volume_filter_box() -> None:
    d = _daily_data()
    vf = d.get("volume_filter") or {}
    st.markdown("**ตัวกรอง volume (ทดสอบบนเดโม)**")
    if not vf:
        st.caption("ยังไม่มีสถิติ (รอรายงานประจำวัน)")
        return
    st.dataframe(pd.DataFrame([{"บอท (MetaQuotes เดโม)": k, **v} for k, v in vf.items()]), width="stretch", hide_index=True)
    st.caption(d.get("volume_filter_note", ""))


def normalized_section(df: pd.DataFrame, df_real: pd.DataFrame | None = None) -> None:
    """เทียบบอทบนฐานเดียวกัน: กำไรต่อไม้ปรับเป็น 0.01 lot (เดโม MQ ใช้ 0.2 lot ตัวเลขดอลลาร์จึงเทียบกับบัญชีจริงตรงๆ ไม่ได้)"""
    frames = [x for x in (df, df_real) if x is not None and len(x)]
    if not frames:
        st.info("ยังไม่มีไม้ที่ปิดแล้ว")
        return
    a = pd.concat(frames, ignore_index=True)
    a = a[a["lot"].notna() & (a["lot"] > 0)].copy()
    if a.empty:
        return
    a["ปรับ 0.01 lot $"] = a["กำไร $"] / (a["lot"] / 0.01)
    rows = []
    for name, g in a.groupby("บอท"):
        days = max((g["ปิดเมื่อ"].max() - g["ปิดเมื่อ"].min()).total_seconds() / 86400, 1.0)
        n = len(g)
        rows.append({"บอท": name, "ไม้": n, "ไม้/วัน": round(n / days, 1), "ชนะ": f"{g['win'].mean():.0%}",
                     "กำไรต่อไม้ (0.01 lot) $": round(g["ปรับ 0.01 lot $"].mean(), 2), "รวม (0.01 lot) $": round(g["ปรับ 0.01 lot $"].sum(), 2),
                     "ความน่าเชื่อถือ": "น้อยเกินสรุป" if n < 30 else "พอใช้" if n < 100 else "ดี"})
    st.markdown("**เทียบบอทบนฐาน 0.01 lot (เทียบกับบัญชีจริงได้)**")
    st.dataframe(pd.DataFrame(rows).sort_values("กำไรต่อไม้ (0.01 lot) $", ascending=False), width="stretch", hide_index=True)
    st.caption("ไม้น้อยกว่า 30 ไม้ = เป็นแค่ตัวชี้ ห้ามใช้ตัดสินว่าบอทตัวไหนดี/แย่ · ไม่รวมสเปรด/สลิปที่ต่างกันของแต่ละโบรกเกอร์")


def real_summary_card(now: datetime) -> None:
    """การ์ดบัญชีจริงบนสุด — แสดงเมื่อใส่ PIN แล้วเท่านั้น (repo นี้เป็นสาธารณะ)"""
    if not real_unlocked():
        return
    docs = list(db.status.find({"server": {"$regex": "^Exness.*real", "$options": "i"}},
                               {"profile": 1, "running": 1, "updated_at": 1, "balance": 1, "equity": 1, "positions": 1, "day_pnl": 1, "paused": 1, "halted_today": 1}))
    if not docs:
        return
    ref = max(docs, key=lambda d: d.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc))
    bal, eq = float(ref.get("balance") or 0), float(ref.get("equity") or 0)
    pos = [dict(p, bot=_pname(str(d.get("profile") or ""))) for d in docs for p in (d.get("positions") or [])]
    day = sum(float(d.get("day_pnl") or 0) for d in docs)
    limit = bal * REAL_DAILY_LOSS_PCT / 100
    online = sum(1 for d in docs if _bot_state(d, now) != "🔴 ออฟไลน์")
    with st.container(border=True):
        st.markdown("**💰 บัญชีจริง (เงินจริง)**")
        m = st.columns(5)
        m[0].metric("Balance", f"{bal:,.2f}")
        m[1].metric("Equity", f"{eq:,.2f}", f"ลอย {sum(float(p.get('profit') or 0) for p in pos):+,.2f}", delta_color="off", delta_arrow="off")
        m[2].metric("วันนี้ รวมทุกบอท (UTC)", f"{day:+,.2f}", f"ลิมิตต่อบอท −${limit:,.0f}", delta_color="off", delta_arrow="off")
        m[3].metric("เป้าถัดไป", f"${REAL_TARGET_BALANCE:g}", f"ห่าง ${REAL_TARGET_BALANCE - bal:,.2f} · ขยับทีละ ${REAL_TARGET_STEP:g}", delta_color="off", delta_arrow="off")
        m[4].metric("บอทจริงออนไลน์", f"{online}/{len(docs)}", f"เส้นหยุด equity ${REAL_STOP_BALANCE:g}", delta_color="off", delta_arrow="off")
        frac = min(max((bal - REAL_STOP_BALANCE) / (REAL_TARGET_BALANCE - REAL_STOP_BALANCE), 0.0), 1.0)
        st.progress(frac, text=f"เส้นหยุด ${REAL_STOP_BALANCE:g} ←→ เป้า ${REAL_TARGET_BALANCE:g} · equity ห่างเส้นหยุด ${eq - REAL_STOP_BALANCE:,.2f}")
        if pos:
            pdf = pd.DataFrame(pos)
            for c in ("side", "volume", "price_open", "sl", "tp", "profit"):
                if c not in pdf:
                    pdf[c] = np.nan
            st.dataframe(pdf.rename(columns={"bot": "บอท", "side": "ทิศ", "volume": "lot", "price_open": "เข้า", "profit": "ลอย $"})[["บอท", "ทิศ", "lot", "เข้า", "sl", "tp", "ลอย $"]],
                         width="stretch", hide_index=True)
        else:
            st.caption("ไม่มีไม้เปิดอยู่")


# ---------- เกณฑ์ผ่านก่อนกลับไปเงินจริง (ตั้ง 2026-10-08) ----------
TEST_START_TH = pd.Timestamp("2026-10-08 22:05")  # เริ่มทดสอบแบบตรึงค่า (เวลาไทย) — ไม้ที่เปิดก่อนนี้ไม่นับ
TEST_PROFILES = ["m15b", "m15sq", "m15roc", "m30b", "m30sq", "m30mom", "m1run", "m5run", "m10run", "m15run", "h1roc", "h4bo"]  # บอทที่อยู่ในการทดสอบ (MetaQuotes เดโม) — เพิ่มสมาชิกกลุ่มใหม่ที่นี่ แต่ละตัวนับเกณฑ์แยกของตัวเอง
PASS_MIN_TRADES, PASS_GOOD_TRADES = 30, 50
PASS_MIN_T = 1.5  # research/results/team_consensus.md: แค่ avgR > 0 บอทที่ไม่มี edge ก็ผ่านได้ ~50% → ต้อง t ≥ 1.5 ด้วย
PASS_MAX_TOP2_SHARE = 0.5  # กำไรจาก 2 ไม้ดีสุดต้องไม่เกินครึ่งของกำไรรวม (ไม่พึ่งไม้ใหญ่ 1-2 ไม้)
STOP_AFTER_N, STOP_AVG_R, STOP_LOSS_STREAK = 15, -0.3, 8  # จุดตัด "แพ้เกินเกณฑ์ ให้หยุดทบทวน"


def _max_streak(wins: list[bool]) -> int:
    best = cur = 0
    for w in wins:
        cur = 0 if w else cur + 1
        best = max(best, cur)
    return best


def pass_criteria_tab() -> None:
    st.subheader("เกณฑ์ผ่าน ก่อนกลับไปเทรดเงินจริง")
    st.caption(f"นับเฉพาะไม้บนเดโม MetaQuotes ที่บอทเปิดเองและปิดเอง (SL/TP/หมดเวลา) ตั้งแต่ {TEST_START_TH:%d/%m/%Y %H:%M} น. · "
               "ห้ามเปลี่ยนค่าบอทระหว่างทดสอบ — ถ้าเปลี่ยน ต้องเริ่มนับใหม่")
    df = trade_frame()
    rows = []
    for prof in TEST_PROFILES:
        d = df[(df["โปรไฟล์"] == prof) & (df["โบรกเกอร์"] == "MetaQuotes") & (df["เปิดเมื่อ"] >= TEST_START_TH)] if not df.empty else df
        manual = int((d["ปิดโดย"] == "ปิดมือ").sum() + (d["ที่มา"] != "signal").sum()) if len(d) else 0
        a = d[(d["ปิดโดย"] != "ปิดมือ") & (d["ที่มา"] == "signal")] if len(d) else d
        n = len(a)
        avg_r = float(a["R"].mean()) if n else float("nan")
        sd = float(a["R"].std(ddof=1)) if n > 1 else float("nan")
        t_stat = avg_r / (sd / n ** 0.5) if sd and sd == sd else float("nan")
        total = float(a["กำไร $"].sum()) if n else 0.0
        top2 = float(a["กำไร $"].nlargest(2).clip(lower=0).sum()) if n else 0.0
        share = top2 / total if total > 0 else float("nan")
        streak_l = _max_streak(list(a["win"])) if n else 0
        checks = {"ไม้ ≥ 30": n >= PASS_MIN_TRADES, "avgR > 0": n > 0 and avg_r > 0, "t ≥ 1.5": t_stat == t_stat and t_stat >= PASS_MIN_T,
                  "ไม่พึ่ง 2 ไม้ใหญ่": total > 0 and share <= PASS_MAX_TOP2_SHARE, "ไม่มีไม้ปิดมือ": manual == 0}
        if (n >= STOP_AFTER_N and avg_r < STOP_AVG_R) or streak_l >= STOP_LOSS_STREAK:
            verdict = "🛑 หยุดทบทวน"
        elif all(checks.values()):
            verdict = "✅ ผ่าน" + (" (ครบ 50 ไม้)" if n >= PASS_GOOD_TRADES else " (แนะนำเก็บให้ครบ 50)")
        elif n < PASS_MIN_TRADES:
            verdict = f"⏳ เก็บข้อมูล {n}/{PASS_MIN_TRADES}"
        else:
            verdict = "❌ ยังไม่ผ่าน"
        rows.append({"บอท": prof, "ผล": verdict, "ไม้": n, "ชนะ %": round(a["win"].mean() * 100) if n else None,
                     "avgR": round(avg_r, 3) if n else None, "t": round(t_stat, 2) if t_stat == t_stat else None, "กำไร $": round(total, 2),
                     "2 ไม้ดีสุด / กำไรรวม": f"{share:.0%}" if share == share else "-", "แพ้ติดกันสูงสุด": streak_l,
                     "ไม้ปิดมือ (ไม่นับ)": manual, **{k: "✅" if v else "·" for k, v in checks.items()}})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    passed = [r["บอท"] for r in rows if r["ผล"].startswith("✅")]
    if passed:
        st.success(f"ผ่านเกณฑ์: {', '.join(passed)} — ขั้นถัดไปคือเงินจริงขนาดเล็ก (บัญชี Cent, เสี่ยง ≤ 1-2%/ไม้) และต้องเป็นคำสั่งของผู้ใช้เอง")
    else:
        st.info("ยังไม่มีบอทผ่านเกณฑ์ — บัญชีจริงคงปิดไว้")
    st.markdown(
        f"""
**เกณฑ์ผ่าน (ต้องครบทุกข้อ ต่อบอท)**
- ไม้ที่บอทเปิดเองและปิดเอง **≥ {PASS_MIN_TRADES} ไม้** (แนะนำ {PASS_GOOD_TRADES})
- **avgR > 0** หลังหักต้นทุนจริง (สเปรด/slippage อยู่ในกำไรสุทธิแล้ว) และ **t ≥ {PASS_MIN_T}** (กันผ่านเพราะดวง)
- กำไรจาก 2 ไม้ดีสุด **≤ {PASS_MAX_TOP2_SHARE:.0%}** ของกำไรรวม
- ไม่มีไม้ปิดมือ และไม่เปลี่ยนค่าระหว่างทดสอบ

**จุดตัด "แพ้เกินเกณฑ์ ให้หยุด"**: ครบ {STOP_AFTER_N} ไม้แล้ว avgR < {STOP_AVG_R} หรือแพ้ติดกัน ≥ {STOP_LOSS_STREAK} ไม้

**กติกาเงินจริงหลังผ่าน**: เสี่ยงไม่เกิน 1-2% ของบัญชีต่อไม้ (ทุนน้อยใช้บัญชี Cent) · ไม่เปิดไม้มือที่ไม่มี SL · ไม่เพิ่ม lot เพื่อเอาคืน
"""
    )


def team_tab() -> None:
    st.subheader("ทีมบอททั้งหมด")
    df = trade_frame()
    t1, t2, t3 = st.tabs(["สถานะ & เป้า", "เทียบบอท", "ไม้ & วิเคราะห์"])
    with t1:
        status_goal_section()
    with t2:
        normalized_section(df, trade_frame(real=True) if real_unlocked() else None)
        compare_section(df)
        volume_filter_box()
        with st.expander("MetaQuotes vs Exness", expanded=False):
            broker_compare_section(df)
    with t3:
        journal_section(df)
        with st.expander("กราฟรวมทุกบอท", expanded=False):
            all_chart_section(df)
        with st.expander("ปิดมือเร็วไปไหม", expanded=False):
            early_close_section(df)


def acct_kind(server: str) -> str:
    s = (server or "").lower()
    if s.startswith("exness"):
        return "Exness จริง" if "real" in s else "Exness Demo"
    return "MetaQuotes Demo"


KINDS = ["MetaQuotes Demo", "Exness Demo", "Exness จริง"]
KIND_NOTE = {"MetaQuotes Demo": "บัญชีเดโม MetaQuotes (เวลาเซิร์ฟเวอร์ = นิวยอร์ก+7) · เป้าทีม +$1,000/+$2,000 นับจากบัญชีนี้",
             "Exness Demo": "บัญชีเดโม Exness (เวลาเซิร์ฟเวอร์ = UTC) · บอทคู่เดโมของบัญชีจริง (ลองเดโมก่อนเสมอ)",
             "Exness จริง": "บัญชีจริง Exness — เงินจริง · ดูได้เมื่อใส่ PIN ที่แถบด้านข้าง"}


def real_unlocked() -> bool:
    return bool(st.session_state.get("real_ok"))


def sidebar_real_unlock() -> None:
    """PIN สำหรับดูข้อมูลบัญชีจริง (อยู่นอก auto-refresh ไม่ให้ PIN ที่พิมพ์หาย) — ไม่มี CONTROL_PIN = ล็อกถาวร"""
    sb = st.sidebar
    sb.divider()
    sb.subheader("ดูบัญชีจริง")
    pin_cfg = secret("CONTROL_PIN")
    if not pin_cfg:
        sb.caption("ล็อกอยู่ — ตั้ง CONTROL_PIN ใน Secrets ก่อน")
        return
    if real_unlocked():
        sb.success("ปลดล็อกแล้ว")
        if sb.button("ล็อกกลับ", key="real_lock"):
            st.session_state["real_ok"] = False
            st.rerun()
        return
    if st.session_state.get("real_fail", 0) >= MAX_PIN_TRIES:
        sb.error("ใส่ PIN ผิดเกินกำหนด — โหลดหน้าใหม่เพื่อลองอีกครั้ง")
        return
    pin = sb.text_input("PIN", type="password", key="real_pin")
    if pin:
        if hmac.compare_digest(pin.encode(), pin_cfg.encode()):
            st.session_state["real_ok"] = True
            st.rerun()
        else:
            st.session_state["real_fail"] = st.session_state.get("real_fail", 0) + 1
            sb.error("PIN ไม่ถูกต้อง")


def _bot_state(d: dict, now: datetime) -> str:
    ua = d.get("updated_at")
    if ua is not None:
        ua = ua if ua.tzinfo else ua.replace(tzinfo=timezone.utc)
    if not d.get("running") or ua is None or (now - ua).total_seconds() > OFFLINE_AFTER_S:
        return "🔴 ออฟไลน์"
    if d.get("paused"):
        return "⏸ หยุดเข้าไม้"
    if d.get("halted_today"):
        return "🛑 ถึงลิมิตวันนี้"
    return "🟢 ทำงาน"


def account_view(kind: str, now: datetime) -> None:
    st.caption(KIND_NOTE[kind])
    is_real = kind == "Exness จริง"
    if is_real and not real_unlocked():
        st.info("🔒 บัญชีจริงถูกซ่อนไว้ — ใส่ PIN ที่แถบด้านข้าง (ดูบัญชีจริง)")
        return
    proj = {"profile": 1, "running": 1, "updated_at": 1, "balance": 1, "equity": 1, "positions": 1, "day_pnl": 1, "entries_today": 1,
            "halted_today": 1, "paused": 1, "config.timeframe": 1, "login": 1, "server": 1}
    docs = [d for d in db.status.find({}, proj) if acct_kind(d.get("server")) == kind]
    if not docs:
        st.info("ยังไม่มีบอทในบัญชีประเภทนี้" + (" (ยังไม่ได้เปิดบอทบัญชีจริง)" if is_real else ""))
        return
    docs.sort(key=lambda d: str(d.get("profile") or ""))
    utc = kind != "MetaQuotes Demo"
    df = trade_frame(real=is_real)
    ids = {d["_id"] for d in docs}
    df = df[df["bot_id"].isin(ids)].copy() if len(df) else df
    ref = max(docs, key=lambda d: d.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc))
    pos_all = [p for d in docs for p in (d.get("positions") or [])]
    floating = sum(float(p.get("profit") or 0) for p in pos_all)
    online = sum(1 for d in docs if _bot_state(d, now) in ("🟢 ทำงาน", "⏸ หยุดเข้าไม้", "🛑 ถึงลิมิตวันนี้"))
    closed_total = float(df["กำไร $"].sum()) if len(df) else 0.0
    m = st.columns(6)
    m[0].metric("Balance", f"{float(ref.get('balance') or 0):,.2f}")
    m[1].metric("Equity", f"{float(ref.get('equity') or 0):,.2f}", f"ลอย {floating:+,.2f}", delta_color="off", delta_arrow="off")
    m[2].metric("บอทออนไลน์", f"{online}/{len(docs)}")
    m[3].metric("ไม้เปิดอยู่", f"{len(pos_all)}")
    m[4].metric("วันนี้ (ปิดแล้ว)", f"{sum(float(d.get('day_pnl') or 0) for d in docs):+,.2f}")
    m[5].metric("สะสมทุกไม้ที่ปิด", f"{closed_total:+,.2f}", f"{len(df)} ไม้", delta_color="off", delta_arrow="off")

    if is_real:  # ความคืบหน้าสู่เป้า/เส้นหยุดของบัญชีจริง (ค่าเดียวกับ BOT_REAL_STOP_BALANCE / BOT_REAL_TARGET_BALANCE ในสคริปต์บอท)
        bal, eq = float(ref.get("balance") or 0), float(ref.get("equity") or 0)
        stop_v, target_v = REAL_STOP_BALANCE, REAL_TARGET_BALANCE
        frac = min(max((bal - stop_v) / (target_v - stop_v), 0.0), 1.0)
        st.progress(frac, text=f"balance {bal:,.2f} · เส้นหยุด ${stop_v:g} (ปิดทุกไม้) ←→ เป้า ${target_v:g} (ปิดทุกไม้) · equity {eq:,.2f} · ห่างเส้นหยุด ${eq - stop_v:,.2f} · ห่างเป้า ${target_v - bal:,.2f}")

    # กราฟ equity ของบัญชี (ใช้ข้อมูลของบอทที่อัปเดตล่าสุด — equity เป็นระดับบัญชี) 14 วันล่าสุด
    try:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)
        eqd = list(db.equity.find({"bot_id": ref["_id"], "time": {"$gte": since}}, {"_id": 0, "time": 1, "equity": 1, "balance": 1}).sort("time", 1))
        if len(eqd) > 2:
            ed = pd.DataFrame(eqd)
            ed["เวลา (ไทย)"] = pd.to_datetime(ed["time"]) + pd.Timedelta(hours=7)
            base = alt.Chart(ed).encode(x=alt.X("เวลา (ไทย):T", title=""))
            st.altair_chart((base.mark_line(color=GOLD).encode(y=alt.Y("equity:Q", title="Equity $", scale=alt.Scale(zero=False)),
                                                                tooltip=[alt.Tooltip("เวลา (ไทย):T", format="%d/%m %H:%M"), alt.Tooltip("equity:Q", format=",.2f"), alt.Tooltip("balance:Q", format=",.2f")])
                             + base.mark_line(color=MUTED, strokeDash=[4, 3]).encode(y="balance:Q")).properties(height=170), width="stretch")
            st.caption("เส้นทอง = equity · เส้นประ = balance (14 วันล่าสุด)")
    except Exception:
        pass
    now_th = pd.Timestamp.now(tz="Asia/Bangkok").tz_localize(None)

    st.markdown("**บอททั้งหมดในบัญชีนี้ (ตัวต่อตัว)**")
    rows = []
    for d in docs:
        b = df[df["bot_id"] == d["_id"]] if len(df) else df
        pos = d.get("positions") or []
        p7 = float(b[b["ปิดเมื่อ"] >= now_th - pd.Timedelta(days=7)]["กำไร $"].sum()) if len(b) else 0.0
        p30 = float(b[b["ปิดเมื่อ"] >= now_th - pd.Timedelta(days=30)]["กำไร $"].sum()) if len(b) else 0.0
        rows.append({
            "7 วัน $": round(p7, 2), "30 วัน $": round(p30, 2),
            "บอท": _pname(str(d.get("profile") or "main")), "สถานะ": _bot_state(d, now), "TF": (d.get("config") or {}).get("timeframe", ""),
            "ไม้เปิด": ", ".join(f"{p.get('side')} {p.get('volume')} @{p.get('price_open')}" for p in pos) or "-",
            "ลอย $": round(sum(float(p.get("profit") or 0) for p in pos), 2), "วันนี้ $": round(float(d.get("day_pnl") or 0), 2),
            "ไม้วันนี้": d.get("entries_today", 0), "ปิดแล้ว $": round(float(b["กำไร $"].sum()), 2) if len(b) else 0.0,
            "ไม้ปิด": len(b), "ชนะ": f"{b['win'].mean():.0%}" if len(b) else "-", "_id": d["_id"],
        })
    tbl = pd.DataFrame(rows)[["บอท", "สถานะ", "TF", "ไม้เปิด", "ลอย $", "วันนี้ $", "7 วัน $", "30 วัน $", "ไม้วันนี้", "ปิดแล้ว $", "ไม้ปิด", "ชนะ", "_id"]]
    st.dataframe(tbl.drop(columns=["_id"]), width="stretch", hide_index=True,
                 column_config={c: st.column_config.NumberColumn(format="%+.2f") for c in ("ลอย $", "วันนี้ $", "7 วัน $", "30 วัน $", "ปิดแล้ว $")})

    st.markdown("**ดูทีละตัว**")
    pick = st.selectbox("เลือกบอท", tbl["_id"].tolist(), format_func=lambda i: next(r["บอท"] + " · " + r["สถานะ"] for r in rows if r["_id"] == i),
                        key=f"acct_pick_{kind}")
    d = next(x for x in docs if x["_id"] == pick)
    b = df[df["bot_id"] == pick].sort_values("ปิดเมื่อ", ascending=False) if len(df) else df
    k = st.columns(5)
    k[0].metric("สถานะ", _bot_state(d, now))
    k[1].metric("ไม้เปิด / ลอย", f"{len(d.get('positions') or [])} ไม้", f"{sum(float(p.get('profit') or 0) for p in (d.get('positions') or [])):+,.2f}",
                delta_color="off", delta_arrow="off")
    k[2].metric("วันนี้", f"{float(d.get('day_pnl') or 0):+,.2f}", f"{d.get('entries_today', 0)} ไม้", delta_color="off", delta_arrow="off")
    k[3].metric("ปิดแล้วสะสม", f"{float(b['กำไร $'].sum()) if len(b) else 0:+,.2f}", f"{len(b)} ไม้", delta_color="off", delta_arrow="off")
    k[4].metric("ชนะ / R เฉลี่ย", f"{b['win'].mean():.0%}" if len(b) else "-", f"{b['R'].mean():+.2f}R" if len(b) and b["R"].notna().any() else "", delta_color="off", delta_arrow="off")

    pos = d.get("positions") or []
    st.markdown("**ไม้ที่เปิดอยู่ตอนนี้**")
    if pos:
        pdf = pd.DataFrame(pos)
        for col in ("ticket", "side", "volume", "price_open", "price_current", "sl", "tp", "profit", "open_time", "entry_reason"):
            if col not in pdf:
                pdf[col] = np.nan
        pdf["เปิดเมื่อ (ไทย)"] = server_to_th(pdf["open_time"], utc=utc).dt.strftime("%d/%m %H:%M")
        st.dataframe(pdf.rename(columns={"ticket": "ticket", "side": "ทิศ", "volume": "lot", "price_open": "เข้า", "price_current": "ราคาตอนนี้",
                                         "profit": "ลอย $", "entry_reason": "เหตุผลเข้า"})[["ticket", "ทิศ", "lot", "เข้า", "ราคาตอนนี้", "sl", "tp", "ลอย $", "เปิดเมื่อ (ไทย)", "เหตุผลเข้า"]],
                     width="stretch", hide_index=True)
    else:
        st.caption("ไม่มีไม้เปิดอยู่")

    st.markdown("**ประวัติไม้ (ใหม่สุดก่อน)**")
    if len(b) == 0:
        st.caption("บอทนี้ยังไม่มีไม้ที่ปิด")
        return
    show = b[["ปิดเมื่อ", "เปิดเมื่อ", "side", "lot", "entry_price", "exit_price", "sl", "กำไร $", "R", "ถือ (นาที)", "ปิดโดย", "exit_reason", "tier", "spread $"]].rename(
        columns={"side": "ทิศ", "entry_price": "เข้า", "exit_price": "ออก", "exit_reason": "เหตุผลปิด"})
    st.dataframe(show, width="stretch", hide_index=True, height=min(420, 38 + 35 * len(show)),
                 column_config={"ปิดเมื่อ": st.column_config.DatetimeColumn(format="DD/MM HH:mm"), "เปิดเมื่อ": st.column_config.DatetimeColumn(format="DD/MM HH:mm"),
                                "กำไร $": st.column_config.NumberColumn(format="%+.2f"), "R": st.column_config.NumberColumn(format="%+.2f")})
    cum = b.sort_values("ปิดเมื่อ")[["ปิดเมื่อ", "กำไร $"]].copy()
    cum["สะสม $"] = cum["กำไร $"].cumsum()
    st.altair_chart(alt.Chart(cum).mark_line(point=True, color=GOLD).encode(
        x=alt.X("ปิดเมื่อ:T", title="เวลา (ไทย)"), y=alt.Y("สะสม $:Q", title="กำไรสะสม $"),
        tooltip=[alt.Tooltip("ปิดเมื่อ:T", format="%d/%m %H:%M"), alt.Tooltip("กำไร $:Q", format="+.2f"), alt.Tooltip("สะสม $:Q", format="+.2f")]).properties(height=220),
        width="stretch")

    st.markdown("**ดูทีละไม้**")
    opts = b.reset_index(drop=True)
    one = st.selectbox("เลือกไม้", list(range(len(opts))), key=f"acct_trade_{kind}_{pick}",
                       format_func=lambda i: f"{opts.loc[i, 'ปิดเมื่อ']:%d/%m %H:%M} · {opts.loc[i, 'side']} {opts.loc[i, 'lot']} · {opts.loc[i, 'กำไร $']:+.2f} $ · {opts.loc[i, 'ปิดโดย']}")
    t = opts.loc[one]
    c1, c2 = st.columns(2)
    c1.markdown(f"**{t['side']} {t['lot']} lot** · เข้า {t['entry_price']} → ออก {t['exit_price']} · SL {t['sl']}  \n"
                f"เปิด {t['เปิดเมื่อ']:%d/%m %H:%M} · ปิด {t['ปิดเมื่อ']:%d/%m %H:%M} · ถือ {t['ถือ (นาที)']:.0f} นาที  \n"
                f"กำไร **{t['กำไร $']:+.2f} $** ({t['R'] if t['R'] == t['R'] else 0:+.2f}R) · ปิดโดย {t['ปิดโดย']} ({t['exit_reason']})")
    c1.caption("เหตุผลเข้า: " + str(t.get("entry_reason") or "-"))
    import json as _j
    try:
        feats = _j.loads(t["features"]) if isinstance(t.get("features"), str) and t["features"] else {}
    except Exception:
        feats = {}
    if feats:
        c2.dataframe(pd.DataFrame({"ตัวแปรตอนเข้าไม้": list(feats.keys()), "ค่า": list(feats.values())}), width="stretch", hide_index=True, height=260)
    else:
        c2.caption("ไม้นี้ไม่มีตัวแปร indicator (ไม้ก่อนเริ่มบันทึก)")


def accounts_tab() -> None:
    st.subheader("แยกตามบัญชี")
    now = datetime.now(timezone.utc)
    tabs = st.tabs(KINDS)
    for tab, kind in zip(tabs, KINDS):
        with tab:
            account_view(kind, now)


@st.fragment(run_every="3s")
def ticker() -> None:
    """แถบสรุปบนสุด: บัญชี MetaQuotes เดโม · ไม้เปิด/กำไรลอย · บอทออนไลน์ · equity · ราคา"""
    now_utc = datetime.now(timezone.utc)
    docs = list(db.status.find({},
                               {"login": 1, "balance": 1, "equity": 1, "bid": 1, "ask": 1, "positions": 1, "day_pnl": 1, "updated_at": 1, "running": 1}))
    if not docs:
        return
    online = sum(1 for d in docs if d.get("running") and d.get("updated_at") is not None and
                 (now_utc - (d["updated_at"] if d["updated_at"].tzinfo else d["updated_at"].replace(tzinfo=timezone.utc))).total_seconds() < 60)
    pos = [p for d in docs for p in (d.get("positions") or [])]
    floating = sum(float(p.get("profit") or 0) for d in docs for p in (d.get("positions") or []))
    ref = max(docs, key=lambda d: d.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc))
    c = st.columns(5)
    c[0].metric("บัญชี", "MetaQuotes เดโม", f"#{ref.get('login', '')}", delta_color="off", delta_arrow="off")
    c[1].metric("ไม้เปิด / กำไรลอย", f"{len(pos)} ไม้", f"{floating:+,.2f} USD", delta_color="off", delta_arrow="off")
    c[2].metric("บอทออนไลน์", f"{online}/{len(docs)}")
    c[3].metric("Equity", f"{float(ref.get('equity') or 0):,.2f}", f"Balance {float(ref.get('balance') or 0):,.2f}", delta_color="off", delta_arrow="off")
    c[4].metric("XAUUSD", f"{float(ref.get('bid') or 0):,.2f}", f"Ask {float(ref.get('ask') or 0):,.2f}", delta_color="off", delta_arrow="off")


def news_market_tab(s: dict, now: datetime) -> None:
    t1, t2, t3 = st.tabs(["รายงานประจำวัน", "ข่าวจากบอท", "สภาพตลาด"])
    with t1:
        daily_report_box()
    with t2:
        news_tab(s, now)
    with t3:
        market_tab(now)


def single_bot_tab(s: dict, now: datetime, trades: pd.DataFrame) -> None:
    st.caption("รายละเอียดของบอทที่เลือกในแถบด้านข้าง (บัญชีรวมทุกตัวดูที่แท็บ 'แยกตามบัญชี' และ 'ทีมบอท')")
    t1, t2, t3 = st.tabs(["ภาพรวม", "กราฟ", "ผลงาน"])
    with t1:
        overview_tab(s, now)
    with t2:
        chart_tab(s, now, trades)
    with t3:
        performance_tab(s, trades)


# ---------- หน้าดูข้อมูลทีม (2026-10-08): สถานะ + เหตุผลเข้า/ออกของทุกไม้ ----------
def exit_kind(reason: str, profit: float) -> str:
    r = str(reason or "")
    if "TP" in r:
        return "✅ เอากำไร (ชน TP)"
    if "SL" in r:
        return "🟢 ล็อกกำไร (SL ที่เลื่อนแล้ว)" if profit > 0 else "🛑 ตัดขาดทุน (ชน SL)"
    if "หมดเวลา" in r:
        return "⏱ หมดเวลาถือ"
    if "deal reason 3" in r or "close all" in r:
        return "🌙 ปิดก่อนปิดคอม 01:00"
    if "มือ" in r or "dashboard" in r:
        return "✋ ปิดมือ"
    return r or "-"


def team_status_section(now: datetime) -> None:
    docs = {d.get("profile"): d for d in db.status.find({"profile": {"$in": TEST_PROFILES}}, {"indicators": 0, "upcoming_news": 0, "recent_news": 0, "params_info": 0, "expected": 0})
            if "metaquotes" in str(d.get("server", "")).lower()}
    if not docs:
        st.info("ยังไม่มีข้อมูลสถานะจากบอท")
        return
    any_doc = next(iter(docs.values()))
    c = st.columns(4)
    c[0].metric("Balance เดโม", f"{any_doc.get('balance', 0):,.2f}")
    c[1].metric("Equity", f"{any_doc.get('equity', 0):,.2f}", f"{any_doc.get('equity', 0) - any_doc.get('balance', 0):+,.2f} ลอย")
    open_n = sum(len(d.get("positions") or []) for d in docs.values())
    c[2].metric("ไม้ที่เปิดอยู่", f"{open_n}")
    c[3].metric("ราคา Bid", f"{any_doc.get('bid', 0):,.2f}")
    rows, opens = [], []
    for prof in TEST_PROFILES:
        d = docs.get(prof)
        if not d:
            rows.append({"บอท": PROFILE_NAMES.get(prof, prof), "สถานะ": "❔ ไม่มีข้อมูล"})
            continue
        age = (now - d["updated_at"]).total_seconds()
        online = d.get("running", False) and age < OFFLINE_AFTER_S
        cfg = d.get("config") or {}
        pos = d.get("positions") or []
        rows.append({"บอท": PROFILE_NAMES.get(prof, prof), "กรอบเวลา": cfg.get("timeframe", ""),
                     "สถานะ": "🟢 ทำงาน" if online else f"🔴 เงียบ {ago(age)}",
                     "lot สูงสุด": cfg.get("max_lot"), "ไม้วันนี้": d.get("entries_today", 0),
                     "กำไรวันนี้ $": round(d.get("day_pnl", 0) or 0, 2),
                     "ถือไม้": ", ".join(f"{p['side']} {p['volume']} ({p['profit']:+.2f})" for p in pos) or "-",
                     "อัปเดต": th_time(d["updated_at"])})
        for p in pos:
            opens.append({"บอท": prof, "ทิศ": p["side"], "lot": p["volume"], "เข้า": p["price_open"], "ตอนนี้": p["price_current"],
                          "SL": p["sl"], "TP": p.get("tp"), "กำไร $": round(p["profit"], 2), "เปิดเมื่อ": p.get("open_time"),
                          "เหตุผลที่เข้า": p.get("entry_reason") or "-"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.markdown("**ไม้ที่เปิดอยู่ และเหตุผลที่เข้า**")
    if opens:
        st.dataframe(pd.DataFrame(opens), hide_index=True, width="stretch")
    else:
        st.caption("ไม่มีไม้ที่เปิดอยู่ — บอทรอสัญญาณตอนแท่งปิด (เข้าไม้ใหม่เฉพาะ 05:00-13:00 และ 19:00-01:00)")


def trades_reason_section() -> None:
    df = trade_frame()
    if df.empty:
        st.info("ยังไม่มีไม้ที่ปิดแล้ว")
        return
    df = df[(df["โบรกเกอร์"] == "MetaQuotes") & df["โปรไฟล์"].isin(TEST_PROFILES)]
    only_test = st.toggle(f"เฉพาะช่วงทดสอบ (ตั้งแต่ {TEST_START_TH:%d/%m %H:%M})", value=True, key="tr_only_test")
    if only_test:
        df = df[df["เปิดเมื่อ"] >= TEST_START_TH]
    if df.empty:
        st.info("ยังไม่มีไม้ที่ปิดในช่วงนี้ — บอทกลุ่มนี้เข้าไม้ไม่บ่อย (ราว 1-2 ไม้/สัปดาห์/ตัว)")
        return
    df = df.copy()
    df["ผลการออก"] = [exit_kind(r, p) for r, p in zip(df["exit_reason"], df["กำไร $"])]
    s = df.groupby("โปรไฟล์").agg(ไม้=("กำไร $", "size"), ชนะ=("win", "sum"), กำไร_รวม=("กำไร $", "sum"), avgR=("R", "mean"))
    kinds = df.pivot_table(index="โปรไฟล์", columns="ผลการออก", values="กำไร $", aggfunc="size", fill_value=0)
    s = s.join(kinds).reset_index().rename(columns={"โปรไฟล์": "บอท", "กำไร_รวม": "กำไร $"})
    s["กำไร $"] = s["กำไร $"].round(2); s["avgR"] = s["avgR"].round(3)
    st.markdown("**สรุปต่อบอท: เอากำไรกี่ครั้ง ตัดขาดทุนกี่ครั้ง**")
    st.dataframe(s, hide_index=True, width="stretch")
    st.markdown("**ทุกไม้ (ล่าสุดก่อน) พร้อมเหตุผลเข้าและออก**")
    show = df.sort_values("ปิดเมื่อ", ascending=False)[["ปิดเมื่อ", "โปรไฟล์", "side", "lot", "entry_price", "exit_price", "กำไร $", "R", "ผลการออก", "entry_reason", "exit_reason", "ถือ (นาที)"]]
    show = show.rename(columns={"โปรไฟล์": "บอท", "side": "ทิศ", "entry_price": "เข้า", "exit_price": "ออก", "entry_reason": "เหตุผลที่เข้า", "exit_reason": "เหตุผลที่ออก (MT5)"})
    st.dataframe(show, hide_index=True, width="stretch",
                 column_config={"ปิดเมื่อ": st.column_config.DatetimeColumn(format="DD/MM HH:mm"), "เหตุผลที่เข้า": st.column_config.TextColumn(width="large")})


@st.fragment(run_every="15s")
def live() -> None:
    now = datetime.now(timezone.utc)
    st.title("ทีมบอท XAUUSD (เดโม)")
    st.caption("ดูข้อมูลอย่างเดียว · ทีมทดสอบ (ตรึงค่า): กลุ่ม M15 m15b m15sq m15roc · กลุ่ม M30 m30b m30sq m30mom · "
               "บอทสำรวจทุกกรอบเวลา: m1run m5run m10run m15run h1roc h4bo · เข้าไม้ใหม่ 05:00-13:00 และ 19:00-01:00")
    t_status, t_trades, t_pass, t_news = st.tabs(["สถานะบอท", "ไม้ & เหตุผล", "เกณฑ์ผ่าน", "ข่าว & ตลาด"])
    with t_status:
        team_status_section(now)
    with t_trades:
        trades_reason_section()
    with t_pass:
        pass_criteria_tab()
    with t_news:
        s = db.status.find_one({"_id": bot_id})
        if s:
            news_market_tab(s, now)


ticker()
live()
