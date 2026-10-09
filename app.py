"""
Dashboard บอท XAUUSD — อ่านข้อมูลที่ bot.py ส่งขึ้น MongoDB (ดู monitor.py)

รันในเครื่อง:   streamlit run dashboard/app.py   (อ่านค่าจาก env หรือ .streamlit/secrets.toml)
ขึ้นออนไลน์:     Streamlit Community Cloud (เปิดดูได้ทุกคนที่มีลิงก์ — ดูข้อมูลอย่างเดียว) → Secrets:
                  MONGODB_URI = "mongodb://dashboard_reader:...@.../"      (user อ่านอย่างเดียว)
"""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from pymongo import DESCENDING, MongoClient
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

TH = timezone(timedelta(hours=7))
SERVER_TZ = "America/New_York"  # MetaQuotes-Demo: เวลาเซิร์ฟเวอร์ = เวลานิวยอร์ก + 7 ชม. (UTC+2/+3 ตาม DST สหรัฐ ไม่ใช่ยุโรป)
OFFLINE_AFTER_S = 60  # บอทอัปเดตทุก ~10 วิ ถ้าเงียบเกินนี้ถือว่าหยุด/คอมดับ/เน็ตหลุด
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
    bot_ids = [d["_id"] for d in _st]
    def _pname(prof: str) -> str:
        return PROFILE_NAMES.get(prof, prof)
    PROFILE_NAMES = {"m30b": "m30b · breakout M30 (ทดสอบ)", "m15b": "m15b · breakout M15 (ทดสอบ)", "m15sq": "m15sq · squeeze M15", "m15roc": "m15roc · momentum M15", "m30sq": "m30sq · squeeze M30", "m30mom": "m30mom · momentum M30", "m1run": "m1run · แท่งสีเดียวกัน M1 (สำรวจ)", "m5run": "m5run · แท่งสีเดียวกัน M5 (สำรวจ)", "m10run": "m10run · แท่งสีเดียวกัน M10 (สำรวจ)", "m15run": "m15run · แท่งสีเดียวกัน M15 (สำรวจ)", "h1roc": "h1roc · โมเมนตัม H1 (สำรวจ)", "h4bo": "h4bo · breakout H4 (สำรวจ)"}
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


# 2026-10-08 (ผู้ใช้สั่ง): ถอดแผงควบคุม/ปุ่มสั่งเข้า-ออกไม้ออก — dashboard ดูข้อมูลอย่างเดียว


# ---------- ส่วนแสดงผล ----------
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


def trade_frame() -> pd.DataFrame:
    """ไม้ที่ปิดแล้วของบอท (เฉพาะบอทที่มองเห็น — Exness ถูกซ่อนที่ _VisibleStatus)"""
    docs = [x for x in load_all_trades() if x.get("bot_id") in server_by_bot]
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
    st.caption(f"อัปเดต {d.get('generated', '-')} (เวลาเครื่อง) · บอทบล็อกก่อน/หลังข่าว 30 นาทีเฉพาะ m15b/m30b และเฉพาะข่าวความสำคัญ ≥3 · บอทตัวอื่นไม่กรองข่าว")


def daily_report_box() -> None:
    d = _daily_data()
    news_blocking_table()
    an = d.get("analysis") or {}
    for title, text in an.items():
        with st.expander(title, expanded=False):
            st.code(text, language=None)
    if an:
        st.caption("ผลวิเคราะห์เทียบข้อมูลราคาย้อนหลัง (SL 7.5 / TP 15) อัปเดตทุกเช้า — เป็นตัวชี้ ไม่ใช่การรับประกันกำไร")


# ---------- เกณฑ์ผ่านก่อนกลับไปเงินจริง (ตั้ง 2026-10-08) ----------
TEST_START_TH = pd.Timestamp("2026-10-09 23:55")  # เริ่มทดสอบแบบตรึงค่า (เวลาไทย) — ไม้ที่เปิดก่อนนี้ไม่นับ
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
        return "🌙 ปิดตอนเลิกเทรด 00:00 (คอมไม่ปิด)"
    if "มือ" in r or "dashboard" in r:
        return "✋ ปิดมือ"
    return r or "-"


# ---------- สรุปวันนี้ + "รออะไรอยู่" (entry_watch.py ส่งขึ้น Mongo ทุกนาทีจาก league.py) ----------
TEAM6 = ["m15b", "m15sq", "m15roc", "m30b", "m30sq", "m30mom"]


def today_strip() -> None:
    """กำไร/ขาดทุนที่ปิดแล้ววันนี้ (เวลาไทย) แยกกลุ่ม: ทีมนับเกณฑ์ 6 · บอทสำรวจ 6 · ลีกส่งออเดอร์จริง 48"""
    today = datetime.now(TH).date()
    df = trade_frame()
    team = expl = 0.0
    nt = ne = 0
    if not df.empty:
        d0 = df[(df["โบรกเกอร์"] == "MetaQuotes") & (df["ปิดเมื่อ"].dt.date == today)]
        a = d0[d0["โปรไฟล์"].isin(TEAM6)]; b = d0[d0["โปรไฟล์"].isin(TEST_PROFILES) & ~d0["โปรไฟล์"].isin(TEAM6)]
        team, nt, expl, ne = float(a["กำไร $"].sum()), len(a), float(b["กำไร $"].sum()), len(b)
    lg = n_lg = 0.0
    try:
        for x in db.league_trades.find({"real": True}, {"_id": 0, "close_time": 1, "real_pnl_usd": 1}).sort("close_time", DESCENDING).limit(400):
            ct = pd.to_datetime(x.get("close_time"), errors="coerce", utc=True)
            if pd.notna(ct) and ct.tz_convert("Asia/Bangkok").date() == today and x.get("real_pnl_usd") not in (None, ""):
                lg += float(x["real_pnl_usd"])
                n_lg += 1
    except Exception:
        pass
    c = st.columns(3)
    c[0].metric("ทีมนับเกณฑ์ 6 ตัว วันนี้", f"{team:+,.2f} $", f"ปิดแล้ว {nt} ไม้", delta_color="off", delta_arrow="off")
    c[1].metric("บอทสำรวจ 6 ตัว วันนี้", f"{expl:+,.2f} $", f"ปิดแล้ว {ne} ไม้", delta_color="off", delta_arrow="off")
    c[2].metric("ลีก 48 ตัว วันนี้ (เฉพาะไม้จริง)", f"{lg:+,.2f} $", f"ปิดแล้ว {int(n_lg)} ไม้", delta_color="off", delta_arrow="off")


def watch_section() -> None:
    w = db.entry_watch.find_one({"_id": "now"})
    st.markdown("**ตอนนี้แต่ละบอทรออะไรอยู่**")
    if not w:
        st.caption("ยังไม่มีข้อมูล (league.py เขียนทุกนาที)")
        return
    age = (datetime.now(timezone.utc) - datetime.fromtimestamp(float(w["ts"]), timezone.utc)).total_seconds()
    h = w.get("htf") or {}
    trend = " · ".join(f"{k} {'↑ ขึ้น' if v.get('dir') == 1 else '↓ ลง'}" for k, v in h.items())
    st.caption(f"ราคา {w.get('price')} · เทรนด์ SMA20/200: {trend} · อัปเดต {ago(age)}" + (" ⚠ ข้อมูลเก่า" if age > 180 else ""))

    def cell(s: dict) -> str:
        if not s:
            return "-"
        if s.get("ready"):
            return "🟢 พร้อมเข้า — " + s.get("text", "")
        return ("⏳ " if s.get("trend_ok") else "⛔ เทรนด์ไม่ผ่าน · ") + s.get("text", "")
    rows = []
    for b in w.get("bots", []):
        sd = b.get("sides") or {}
        rows.append({"บอท": PROFILE_NAMES.get(b["bot"], b["bot"]), "TF": b.get("tf"), "ต้องการเทรนด์": "+".join(b.get("need") or []) or "ไม่กรอง",
                     "SELL": cell(sd.get("SELL")), "BUY": cell(sd.get("BUY"))})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={"SELL": st.column_config.TextColumn(width="large"), "BUY": st.column_config.TextColumn(width="large")})
    st.caption("🟢 เทรนด์ผ่านและราคาถึงจุดเข้าแล้ว (จะเปิดในแท่งที่ปิดถัดไป ถ้าไม่ติดเพดาน/ช่วงเวลา) · ⏳ เทรนด์ผ่านแต่ราคายังไม่ถึงจุดเข้า · ⛔ เทรนด์ไม่ผ่าน บอทจะไม่เข้าฝั่งนั้นต่อให้ราคาถึง")


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
    today_strip()
    watch_section()
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
        st.caption("ไม่มีไม้ที่เปิดอยู่ — บอทรอสัญญาณตอนแท่งปิด (เข้าไม้ใหม่ได้ทั้งวัน ยกเว้น 00:00-05:00)")


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
    # ใช้ชื่อคอลัมน์อังกฤษตอนคำนวณ แล้วเปลี่ยนเป็นไทย — ชื่อ kwarg ภาษาไทยถูก Python แปลงรูปสระ (ำ→ํา) จนไม่ตรงกับสตริง
    s = df.groupby("โปรไฟล์").agg(n=("กำไร $", "size"), wins=("win", "sum"), profit=("กำไร $", "sum"), avgR=("R", "mean"))
    kinds = df.pivot_table(index="โปรไฟล์", columns="ผลการออก", values="กำไร $", aggfunc="size", fill_value=0)
    s = s.join(kinds).reset_index().rename(columns={"โปรไฟล์": "บอท", "n": "ไม้", "wins": "ชนะ", "profit": "กำไร $"})
    s["กำไร $"] = s["กำไร $"].round(2); s["avgR"] = s["avgR"].round(3)
    st.markdown("**สรุปต่อบอท: เอากำไรกี่ครั้ง ตัดขาดทุนกี่ครั้ง**")
    st.dataframe(s, hide_index=True, width="stretch")
    st.markdown("**ทุกไม้ (ล่าสุดก่อน) พร้อมเหตุผลเข้าและออก**")
    show = df.sort_values("ปิดเมื่อ", ascending=False)[["ปิดเมื่อ", "โปรไฟล์", "side", "lot", "entry_price", "exit_price", "กำไร $", "R", "ผลการออก", "entry_reason", "exit_reason", "ถือ (นาที)"]]
    show = show.rename(columns={"โปรไฟล์": "บอท", "side": "ทิศ", "entry_price": "เข้า", "exit_price": "ออก", "entry_reason": "เหตุผลที่เข้า", "exit_reason": "เหตุผลที่ออก (MT5)"})
    st.dataframe(show, hide_index=True, width="stretch",
                 column_config={"ปิดเมื่อ": st.column_config.DatetimeColumn(format="DD/MM HH:mm"), "เหตุผลที่เข้า": st.column_config.TextColumn(width="large")})



# ---------- ลีก 48 ตัว (league.py: จำลองด้วยราคา bid/ask สด + ส่งออเดอร์จริงบนเดโมสูงสุด 24 ไม้พร้อมกัน) ----------
def league_tab() -> None:
    st.subheader("ลีกบอท 48 ตัว: แข่งกันทำกำไร (ส่งออเดอร์จริงบนเดโม)")
    st.caption("48 ตัวในโปรเซสเดียว ส่งออเดอร์จริงบนบัญชีเดโม (magic 20262001–20262048, lot 0.10, SL ที่เซิร์ฟเวอร์, เปิดพร้อมกันไม่เกิน 24 ไม้ — ตัวที่เกินเพดานเล่นแบบเสมือน) ผลสดใช้กำไรจริงจากโบรก · 12 ฐาน = ตัวแปรของบอทจริง (ต่างเล็กน้อย/ต่างมาก) × ซื้อ/ขายแยก × เข้าทันที(I)/รอแท่งยืนยัน(C) · "
               "ผลย้อนหลังคือ HOLDOUT ตั้งแต่ 1 ต.ค. 2025 ต้นทุน $0.45/oz · แข่ง 48 ตัวแล้วเลือกผู้ชนะ มีโชคปน ดูผลสดไปข้างหน้าเป็นหลัก")
    docs = list(db.league_stats.find({}))
    if not docs:
        st.info("ยังไม่มีข้อมูลลีก — เริ่ม scripts\run_league.bat")
        return
    rows = []
    for d in docs:
        lv, bt = d.get("live") or {}, (d.get("backtest") or {})
        hd, dv = bt.get("hold") or {}, bt.get("dev") or {}
        rows.append({"ตัว": d["_id"], "ฐาน": d.get("strat"), "TF": d.get("tf"), "ฝั่ง": {"B": "BUY", "S": "SELL"}.get(d.get("side"), d.get("side")),
                     "เข้า": {"I": "ทันที", "C": "รอยืนยัน"}.get(d.get("mode"), d.get("mode")),
                     "สด ไม้": lv.get("n", 0), "สด ชนะ %": round(100 * lv.get("wins", 0) / lv["n"]) if lv.get("n") else None,
                     "สด กำไร $": round(lv.get("net_usd", 0.0), 2), "สด $/oz": round(lv.get("per_oz", 0.0), 2),
                     "สด DD $": round(lv.get("max_dd_usd", 0.0), 2), "ถือไม้": "✅" if lv.get("open") else "", "ลอย $": round(lv.get("open_pnl_usd", 0.0), 2),
                     "ย้อนหลัง HOLD ไม้": hd.get("n"), "HOLD $/oz": hd.get("per_oz"), "HOLD t": hd.get("t"),
                     "DEV $/oz": dv.get("per_oz"), "โชค %ile (DEV)": bt.get("luck_pct")})
    df = pd.DataFrame(rows)
    live_total = df["สด กำไร $"].sum()
    c = st.columns(4)
    c[0].metric("ตัวที่แข่ง", f"{len(df)}")
    c[1].metric("ไม้สดที่ปิดแล้วรวม", f"{int(df['สด ไม้'].sum())}")
    c[2].metric("กำไรสดรวม (0.10 lot เสมือน)", f"{live_total:+,.2f} $")
    c[3].metric("ตัวที่ถือไม้อยู่", f"{int((df['ถือไม้'] == '✅').sum())}")
    t1, t2, t3, t4 = st.tabs(["อันดับสด", "อันดับย้อนหลัง", "ไม้สดล่าสุด", "จริงเทียบโมเดล"])
    with t1:
        s = df.sort_values(["สด กำไร $", "สด ไม้"], ascending=[False, False])
        st.dataframe(s[["ตัว", "TF", "ฝั่ง", "เข้า", "สด ไม้", "สด ชนะ %", "สด กำไร $", "สด $/oz", "สด DD $", "ถือไม้", "ลอย $", "HOLD $/oz"]], hide_index=True, width="stretch")
        if int(df["สด ไม้"].sum()) == 0:
            st.caption("ยังไม่มีไม้สดที่ปิด — ลีกเปิดไม้ใหม่ได้ทั้งวัน ยกเว้น 00:00-05:00 เวลาไทย · ปิดทุกไม้ตอน 00:00 (งานปิดประจำคืน) ")
    with t2:
        s = df.sort_values("HOLD $/oz", ascending=False)
        st.dataframe(s[["ตัว", "ฐาน", "ฝั่ง", "เข้า", "ย้อนหลัง HOLD ไม้", "HOLD $/oz", "HOLD t", "DEV $/oz", "โชค %ile (DEV)"]], hide_index=True, width="stretch")
        st.caption("เกณฑ์ผ่านแบบ Bonferroni (48 ตัว) ต้อง t ≥ 3.08 · ทองขึ้นแรงช่วง HOLDOUT ทำให้เกือบทุกตัวบวก ไม่ใช่ความแข็งของสัญญาณ · 'โชค %ile' สูง = ผลดีกว่าการสุ่มเข้า")
    with t3:
        tr = pd.DataFrame(list(db.league_trades.find({}, {"_id": 0}).sort("close_time", DESCENDING).limit(200)))
        if tr.empty:
            st.info("ยังไม่มีไม้สดที่ปิด")
        else:
            cols = [x for x in ("close_time", "variant", "side", "entry", "exit", "pnl_usd", "pnl_per_oz", "hold_min", "exit_reason", "signal_reason") if x in tr.columns]
            st.dataframe(tr[cols], hide_index=True, width="stretch")
    with t4:
        league_real_vs_model()


# ---------- กราฟกำไรสะสม + ผลจริงเทียบโมเดลของลีก ----------
def _profit_rows(include_virtual: bool) -> pd.DataFrame:
    """ไม้ที่ปิดแล้วทุกกลุ่ม → (เวลาปิดไทย, กลุ่ม, บอท, กำไร $): ทีมนับเกณฑ์ 6 · บอทสำรวจ 6 · ลีก (เฉพาะไม้จริง หรือรวมเสมือนถ้าเลือก)"""
    rows = []
    df = trade_frame()
    if not df.empty:
        d = df[(df["โบรกเกอร์"] == "MetaQuotes") & df["โปรไฟล์"].isin(TEST_PROFILES)]
        for _, r in d.iterrows():
            rows.append((r["ปิดเมื่อ"], "ทีมนับเกณฑ์ 6" if r["โปรไฟล์"] in TEAM6 else "บอทสำรวจ 6", r["โปรไฟล์"], float(r["กำไร $"])))
    for x in db.league_trades.find({}, {"_id": 0, "variant": 1, "close_time": 1, "pnl_usd": 1, "real": 1, "real_pnl_usd": 1}):
        ct = pd.to_datetime(x.get("close_time"), errors="coerce", utc=True)
        if pd.isna(ct):
            continue
        real = bool(x.get("real")) and x.get("real_pnl_usd") not in (None, "")
        if not real and not include_virtual:
            continue
        rows.append((ct.tz_convert("Asia/Bangkok").tz_localize(None), "ลีก (จริง)" if real else "ลีก (เสมือน)", x.get("variant"),
                     float(x["real_pnl_usd"]) if real else float(x.get("pnl_usd") or 0.0)))
    cols = ["เวลา", "กลุ่ม", "บอท", "กำไร"]
    return pd.DataFrame(rows, columns=cols).sort_values("เวลา").reset_index(drop=True) if rows else pd.DataFrame(columns=cols)


def profit_tab() -> None:
    st.subheader("กำไรสะสม")
    inc = st.toggle("รวมไม้เสมือนของลีกด้วย (ไม้ที่เกินเพดาน 24 ไม้ หรือส่งออเดอร์ไม่สำเร็จ)", value=False, key="pf_inc_virtual")
    d = _profit_rows(inc)
    if d.empty:
        st.info("ยังไม่มีไม้ที่ปิด — กราฟจะขึ้นเมื่อมีไม้แรก (ทีมนับเกณฑ์ / บอทสำรวจ / ลีกจริง)")
        return
    d["สะสม"] = d.groupby("กลุ่ม")["กำไร"].cumsum()
    tot = d.copy()
    tot["กลุ่ม"] = "รวมทั้งหมด"
    tot["สะสม"] = tot["กำไร"].cumsum()
    both = pd.concat([d, tot], ignore_index=True)
    c = st.columns(4)
    c[0].metric("กำไรสะสมรวม", f"{d['กำไร'].sum():+,.2f} $")
    c[1].metric("ไม้ที่ปิดแล้ว", f"{len(d)}")
    c[2].metric("ชนะ", f"{(d['กำไร'] > 0).mean() * 100:.0f}%")
    dd = (tot["สะสม"].cummax() - tot["สะสม"]).max()
    c[3].metric("drawdown สูงสุด", f"{dd:,.2f} $")
    st.altair_chart(alt.Chart(both).mark_line(interpolate="step-after").encode(
        x=alt.X("เวลา:T", title=None), y=alt.Y("สะสม:Q", title="กำไรสะสม (USD)"), color=alt.Color("กลุ่ม:N", title=None),
        tooltip=["เวลา:T", "กลุ่ม:N", "บอท:N", alt.Tooltip("กำไร:Q", format="+,.2f"), alt.Tooltip("สะสม:Q", format="+,.2f")]).properties(height=320), width="stretch")
    d["วัน"] = pd.to_datetime(d["เวลา"]).dt.date
    daily = d.groupby(["วัน", "กลุ่ม"], as_index=False)["กำไร"].sum()
    st.markdown("**กำไรรายวัน (เวลาไทย)**")
    st.altair_chart(alt.Chart(daily).mark_bar().encode(x=alt.X("วัน:T", title=None), y=alt.Y("กำไร:Q", title="USD"), color=alt.Color("กลุ่ม:N", title=None),
                                                       tooltip=["วัน:T", "กลุ่ม:N", alt.Tooltip("กำไร:Q", format="+,.2f")]).properties(height=220), width="stretch")
    per = d.groupby(["กลุ่ม", "บอท"]).agg(n=("กำไร", "size"), wins=("กำไร", lambda x: int((x > 0).sum())), profit=("กำไร", "sum")).reset_index()
    per["ชนะ %"] = (per["wins"] / per["n"] * 100).round(0)
    per = per.rename(columns={"n": "ไม้", "profit": "กำไร $"}).drop(columns="wins").sort_values("กำไร $", ascending=False)
    per["กำไร $"] = per["กำไร $"].round(2)
    st.markdown("**แยกตามบอท**")
    st.dataframe(per, hide_index=True, width="stretch")


def league_real_vs_model() -> None:
    st.markdown("**ผลจริงเทียบโมเดล (ไม้ลีกที่ส่งออเดอร์จริง)**")
    docs = list(db.league_trades.find({}, {"_id": 0}))
    if not docs:
        st.info("ยังไม่มีไม้ลีกที่ปิด")
        return
    d = pd.DataFrame(docs)
    if "real" not in d:
        d["real"] = False
    d["real"] = d["real"].fillna(False).astype(bool)
    n_all, n_real = len(d), int(d["real"].sum())
    st.caption(f"ไม้ลีกที่ปิดแล้วทั้งหมด {n_all} · ส่งออเดอร์จริง {n_real} · เสมือนอย่างเดียว {n_all - n_real} (เกินเพดาน / ช่วงเวลาห้ามเข้า / สเปรดกว้าง / ออเดอร์ไม่สำเร็จ)")
    r = d[d["real"]].copy()
    if r.empty:
        st.info("ยังไม่มีไม้จริงที่ปิด — ตารางนี้จะเทียบกำไรที่โบรกจ่ายจริงกับที่โมเดลคำนวณ เพื่อดูต้นทุนที่โมเดลมองไม่เห็น (ราคาเลื่อน สเปรด swap)")
        return
    for col in ("pnl_usd", "real_pnl_usd", "entry", "real_entry", "exit", "real_exit"):
        r[col] = pd.to_numeric(r[col], errors="coerce")
    sgn = r["side"].map({"BUY": 1, "SELL": -1})
    r["ต่าง $"] = (r["real_pnl_usd"] - r["pnl_usd"]).round(2)
    r["เข้าเลื่อน $/oz"] = ((r["real_entry"] - r["entry"]) * sgn * -1).round(3)   # + = เข้าได้ราคาดีกว่าโมเดล
    r["ออกเลื่อน $/oz"] = ((r["real_exit"] - r["exit"]) * sgn).round(3)           # + = ออกได้ราคาดีกว่าโมเดล
    c = st.columns(4)
    c[0].metric("ผลโมเดลรวม", f"{r['pnl_usd'].sum():+,.2f} $")
    c[1].metric("ผลจริงรวม", f"{r['real_pnl_usd'].sum():+,.2f} $")
    c[2].metric("ส่วนต่างรวม (จริง − โมเดล)", f"{r['ต่าง $'].sum():+,.2f} $", f"เฉลี่ย {r['ต่าง $'].mean():+.2f} $/ไม้", delta_color="off", delta_arrow="off")
    c[3].metric("เลื่อนเฉลี่ย เข้า / ออก", f"{r['เข้าเลื่อน $/oz'].mean():+.2f} / {r['ออกเลื่อน $/oz'].mean():+.2f} $/oz")
    g = r.groupby("variant").agg(n=("pnl_usd", "size"), model=("pnl_usd", "sum"), real=("real_pnl_usd", "sum")).reset_index()
    g["ต่าง $"] = g["real"] - g["model"]
    g = g.rename(columns={"variant": "ตัว", "n": "ไม้จริง", "model": "โมเดล $", "real": "จริง $"}).round(2).sort_values("จริง $", ascending=False)
    st.dataframe(g, hide_index=True, width="stretch")
    st.markdown("**ไม้จริงล่าสุด**")
    show = r.sort_values("close_time", ascending=False).head(100)[["close_time", "variant", "side", "entry", "real_entry", "exit", "real_exit", "pnl_usd", "real_pnl_usd", "ต่าง $", "exit_reason"]]
    st.dataframe(show, hide_index=True, width="stretch")
    st.caption("ส่วนต่างติดลบสม่ำเสมอ = โมเดลประเมินต้นทุนต่ำไป (ราคาเลื่อน สเปรดจริงช่วงนั้น swap) · อันดับลีกใช้ผลจริงของไม้จริงเป็นหลัก")


def exit_methods_section(dx: pd.DataFrame) -> None:
    """ชุดทดสอบวิธีออกไม้ (x<กรอบ><วิธี><ค่า>): เข้าไม้เหมือนกัน (3 แท่งสีเดียวกัน) ต่างกันที่ TP / เลื่อน SL คุ้มทุน / trailing / ระยะ SL"""
    st.markdown("---")
    st.markdown("**ชุดทดสอบวิธีออกไม้ (x 140 ตัว = 20 ต่อกรอบเวลา)** — เข้าไม้เหมือนกันหมด (3 แท่งสีเดียวกัน) ต่างที่วิธีออก: "
                "`tp` TP = ค่า × SL ฐาน · `lk` ถึง +ค่า × SL แล้วเลื่อน SL มาคุ้มทุน · `tr` trailing ตาม ATR (ค่า = เท่าของ ATR) · `sl` SL = ค่า × SL ฐาน (TP ตามสัดส่วน) · ค่าเป็นเท่าของ SL ฐานของกรอบนั้น จึงเทียบข้ามกรอบได้ · ถือสูงสุด 60 แท่ง")
    if dx.empty:
        st.info("ชุดวิธีออกไม้ยังไม่มีไม้ที่ปิด")
        return
    g = dx.groupby(["โปรไฟล์", "tf", "method", "param"], dropna=False)
    t = g.agg(n=("R", "size"), wins=("win", "sum"), profit=("กำไร $", "sum"), avgR=("R", "mean")).reset_index()
    t["ชนะ %"] = (t["wins"] / t["n"] * 100).round(0)
    t = t.rename(columns={"โปรไฟล์": "บอท", "tf": "กรอบ", "method": "วิธี", "param": "ค่า", "n": "ไม้", "profit": "กำไร $"})
    st.dataframe(t.sort_values("avgR", ascending=False)[["บอท", "กรอบ", "วิธี", "ค่า", "ไม้", "ชนะ %", "กำไร $", "avgR"]].round(2), hide_index=True, use_container_width=True)


def signal_section(d: pd.DataFrame) -> None:
    """ชุดสัญญาณอื่น: d = Donchian breakout (d<กรอบ>n<lookback>h<ถือ>), r = momentum ตัด ±1 (r<กรอบ>n<n>h<ถือ>) — ตามทิศ ไม่กรองเทรนด์ ใช้สัญญาณชุดเดียวกับบอทจริง"""
    st.markdown("---")
    st.markdown("**ชุดสัญญาณอื่น (168 ตัว = Donchian 12 + momentum 12 ต่อกรอบเวลา)** — `d` Donchian breakout (ปิดทะลุ high/low n แท่ง) · `r` momentum z ตัด ±1 · ทุกกรอบเวลา M1-H4 · ถือ 5-30 แท่ง")
    if d.empty:
        st.info("ชุดสัญญาณอื่นยังไม่มีไม้ที่ปิด (กรอบใหญ่ H1/H4 ไม้น้อยมาก รอสะสม)")
        return
    t = d.groupby(["โปรไฟล์", "family", "tf"]).agg(n=("R", "size"), wins=("win", "sum"), profit=("กำไร $", "sum"), avgR=("R", "mean")).reset_index()
    t["ชนะ %"] = (t["wins"] / t["n"] * 100).round(0)
    t = t.rename(columns={"โปรไฟล์": "บอท", "family": "ชุด", "tf": "กรอบ", "n": "ไม้", "profit": "กำไร $"})
    st.dataframe(t.sort_values("avgR", ascending=False)[["บอท", "ชุด", "กรอบ", "ไม้", "ชนะ %", "กำไร $", "avgR"]].round(2), hide_index=True, use_container_width=True)


def fade_section(d: pd.DataFrame) -> None:
    """ชุดสวนทิศ (f<กรอบ>n<แท่ง>h<ถือ>): เห็น N แท่งสีเดียวกันแล้วเข้าสวน — เทียบกับชุดตามทิศ (v)"""
    st.markdown("---")
    st.markdown("**ชุดสวนทิศ (fade, 112 ตัว)** — เห็น N แท่งสีเดียวกันแล้วเข้าฝั่งตรงข้าม (ชื่อ f<กรอบ>n<แท่ง>h<ถือ>)")
    if d.empty:
        st.info("ชุดสวนทิศยังไม่มีไม้ที่ปิด")
        return
    t = d.groupby(["โปรไฟล์", "tf"]).agg(n=("R", "size"), wins=("win", "sum"), profit=("กำไร $", "sum"), avgR=("R", "mean")).reset_index()
    t["ชนะ %"] = (t["wins"] / t["n"] * 100).round(0)
    t = t.rename(columns={"โปรไฟล์": "บอท", "tf": "กรอบ", "n": "ไม้", "profit": "กำไร $"})
    st.dataframe(t.sort_values("avgR", ascending=False)[["บอท", "กรอบ", "ไม้", "ชนะ %", "กำไร $", "avgR"]].round(2), hide_index=True, use_container_width=True)


def variants_tab() -> None:
    """ฝูงบอทเสมือน (swarm.py ในโปรเซสลีก): เทียบกรอบเวลา × จำนวนแท่งสีเดียวกัน × ถือกี่แท่ง — บอทสำรวจ ไม่นับในเกณฑ์ผ่าน"""
    st.caption("ฝูงบอทเสมือน 700 ตัว จำลอง (paper) ไม่ส่งออเดอร์ (ชื่อ v<กรอบ>n<แท่งสีเดียวกัน>h<ถือกี่แท่ง> เช่น v1n3h5 = M1, 3 แท่ง, ถือ ≤ 5 แท่ง) · lot 0.02 · ทดสอบเพื่อหาว่าถือสั้น/นานแบบไหนดี · "
               "ระวัง: ลอง 700 แบบ ตัวที่ดูดีสุดมักเป็นโชค — ดูตัวที่มีไม้ ≥ 30 และดูทั้งแถบ (กลุ่ม) ไม่ใช่ตัวเดียว")
    # swarm.py ส่งไม้เสมือนที่ปิดแล้วขึ้น collection variant_trades
    docs = list(db.variant_trades.find({}, {"_id": 0, "profile": 1, "tf": 1, "run_n": 1, "hold": 1, "net_profit": 1, "r_multiple": 1, "family": 1, "method": 1, "param": 1}))
    if not docs:
        st.info("ฝูงบอทเสมือนยังไม่มีไม้ที่ปิด — รอสัญญาณ (ถือสั้น M1 จะมีไม้เร็วสุด)")
        return
    df = pd.DataFrame(docs).rename(columns={"profile": "โปรไฟล์", "run_n": "run_n", "hold": "hold"})
    df["กำไร $"] = pd.to_numeric(df["net_profit"], errors="coerce")
    df["R"] = pd.to_numeric(df["r_multiple"], errors="coerce")
    df = df.dropna(subset=["กำไร $"])
    df["win"] = df["กำไร $"] > 0
    fam = df["family"].fillna("v") if "family" in df else pd.Series("v", index=df.index)
    dx, dfade, dsig = df[fam.isin(["x", "t"])].copy(), df[fam == "f"].copy(), df[fam.isin(["d", "r"])].copy()
    df = df[~fam.isin(["x", "t", "f", "d", "r"])].copy()
    if df.empty:
        st.info("ชุดตามทิศ (v) ยังไม่มีไม้ที่ปิด")
        exit_methods_section(dx)
        fade_section(dfade)
        signal_section(dsig)
        return
    keys = ["โปรไฟล์", "tf", "run_n", "hold"]
    g = df.groupby(keys)
    s = g.agg(n=("R", "size"), wins=("win", "sum"), profit=("กำไร $", "sum"), avgR=("R", "mean"), sdR=("R", "std")).reset_index()
    s["t"] = np.where((s["n"] > 2) & (s["sdR"] > 0), s["avgR"] / (s["sdR"] / np.sqrt(s["n"])), np.nan)
    s["winpct"] = (s["wins"] / s["n"] * 100).round(0)
    min_n = st.slider("แสดงเฉพาะบอทที่มีไม้อย่างน้อย", 1, 100, 10, key="var_min_n")
    t = s[s["n"] >= min_n].sort_values("avgR", ascending=False)
    c1, c2, c3 = st.columns(3)
    c1.metric("ไม้ที่ปิดแล้วทั้งหมด", f"{int(s['n'].sum()):,}")
    c2.metric("กำไรรวม", f"{s['profit'].sum():+,.0f} $")
    c3.metric("บอทที่มีไม้แล้ว", f"{len(s)}/100 (ชุดตามทิศ)")
    st.markdown("**อันดับ (เรียงตาม avgR)**")
    out = t.rename(columns={"โปรไฟล์": "บอท", "tf": "กรอบ", "run_n": "แท่งสีเดียวกัน", "hold": "ถือ ≤ (แท่ง)", "n": "ไม้", "winpct": "ชนะ %",
                            "profit": "กำไร $", "avgR": "avgR", "t": "t"})[["บอท", "กรอบ", "แท่งสีเดียวกัน", "ถือ ≤ (แท่ง)", "ไม้", "ชนะ %", "กำไร $", "avgR", "t"]]
    st.dataframe(out.round(2), hide_index=True, use_container_width=True)
    st.markdown("**ถือกี่แท่งดีที่สุด? (รวมทุกบอทที่ถือเท่ากัน แยกตามกรอบ) — avgR**")
    s["_rs"] = s["avgR"] * s["n"]
    pv = s.groupby(["tf", "hold"]).agg(rs=("_rs", "sum"), n=("n", "sum")).reset_index()
    pv["avgR"] = pv["rs"] / pv["n"]
    pv = pv[pv["n"] >= 5]
    if len(pv):
        st.dataframe(pv.pivot(index="hold", columns="tf", values="avgR").round(2), use_container_width=True)
    st.markdown("**แท่งสีเดียวกันกี่แท่งดีที่สุด? — avgR**")
    pr = s.groupby(["tf", "run_n"]).agg(rs=("_rs", "sum"), n=("n", "sum")).reset_index()
    pr["avgR"] = pr["rs"] / pr["n"]
    pr = pr[pr["n"] >= 5]
    if len(pr):
        st.dataframe(pr.pivot(index="run_n", columns="tf", values="avgR").round(2), use_container_width=True)
    exit_methods_section(dx)
    fade_section(dfade)
    signal_section(dsig)


@st.fragment(run_every="15s")
def live() -> None:
    now = datetime.now(timezone.utc)
    st.title("ทีมบอท XAUUSD (เดโม)")
    st.caption("ดูข้อมูลอย่างเดียว · ทีมทดสอบ (ตรึงค่า): กลุ่ม M15 m15b m15sq m15roc · กลุ่ม M30 m30b m30sq m30mom · "
               "บอทสำรวจทุกกรอบเวลา: m1run m5run m10run m15run h1roc h4bo · เข้าไม้ใหม่ได้ทั้งวัน ยกเว้น 00:00-05:00")
    t_status, t_trades, t_profit, t_pass, t_league, t_var, t_news = st.tabs(["สถานะบอท", "ไม้ & เหตุผล", "กราฟกำไร", "เกณฑ์ผ่าน", "ลีก 48 ตัว", "ฝูงเสมือน 700 ตัว", "ข่าว & ตลาด"])
    with t_status:
        team_status_section(now)
    with t_trades:
        trades_reason_section()
    with t_profit:
        profit_tab()
    with t_pass:
        pass_criteria_tab()
    with t_league:
        league_tab()
    with t_var:
        variants_tab()
    with t_news:
        s = db.status.find_one({"_id": bot_id})
        if s:
            news_market_tab(s, now)


ticker()
live()
