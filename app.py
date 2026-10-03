"""
Dashboard บอท XAUUSD — อ่านข้อมูลที่ bot.py ส่งขึ้น MongoDB (ดู monitor.py)

รันในเครื่อง:   streamlit run dashboard/app.py   (อ่าน MONGODB_URI จาก env หรือ .streamlit/secrets.toml)
ขึ้นออนไลน์:     Streamlit Community Cloud → ตั้ง Secrets:
                  MONGODB_URI = "mongodb+srv://dashboard_reader:...@.../"   (user แบบอ่านอย่างเดียว)
                  DASHBOARD_PASSWORD = "..."
"""
import hmac
import os
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st
from pymongo import DESCENDING, MongoClient
from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

TH = timezone(timedelta(hours=7))
OFFLINE_AFTER_S = 60  # บอทอัปเดตทุก ~10 วิ ถ้าเงียบเกินนี้ถือว่าหยุด/คอมดับ/เน็ตหลุด

st.set_page_config(page_title="XAUUSD Bot", page_icon="📈", layout="wide")


def secret(name: str, default: str = "") -> str:
    try:
        return st.secrets[name]
    except Exception:
        return os.getenv(name, default)


def require_password() -> None:
    pw = secret("DASHBOARD_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    st.title("XAUUSD Bot")
    with st.form("login"):
        entered = st.text_input("รหัสผ่าน", type="password")
        if st.form_submit_button("เข้าสู่ระบบ"):
            if hmac.compare_digest(entered.encode(), pw.encode()):
                st.session_state.authed = True
                st.rerun()
            st.error("รหัสผ่านไม่ถูกต้อง")
    st.stop()


@st.cache_resource
def get_db():
    client = MongoClient(secret("MONGODB_URI"), serverSelectionTimeoutMS=5000, tz_aware=True)
    return client[secret("MONGODB_DB", "xauusd_bot")]


def th_time(dt: datetime) -> str:
    return dt.astimezone(TH).strftime("%d/%m %H:%M:%S")


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} วินาทีที่แล้ว"
    if seconds < 5400:
        return f"{seconds / 60:.0f} นาทีที่แล้ว"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} ชั่วโมงที่แล้ว"
    return f"{seconds / 86400:.0f} วันที่แล้ว"


require_password()
if not secret("MONGODB_URI"):
    st.error("ยังไม่ได้ตั้ง MONGODB_URI — ใส่ใน Secrets ของ Streamlit หรือ environment variable")
    st.stop()
db = get_db()

try:
    bot_ids = [d["_id"] for d in db.status.find({}, {"_id": 1})]
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
bot_id = st.sidebar.selectbox("บอท (login-magic)", bot_ids) if len(bot_ids) > 1 else bot_ids[0]
days = st.sidebar.select_slider("กราฟย้อนหลัง", options=[1, 7, 30, 90, 365], value=30, format_func=lambda d: f"{d} วัน")


@st.fragment(run_every="15s")
def live() -> None:
    s = db.status.find_one({"_id": bot_id})
    now = datetime.now(timezone.utc)
    age = (now - s["updated_at"]).total_seconds()
    online = s.get("running", False) and age < OFFLINE_AFTER_S
    cfg = s.get("config", {})
    cur = s.get("currency", "USD")

    # ---------- หัว + สถานะ ----------
    left, right = st.columns([3, 2], vertical_alignment="bottom")
    left.title(f"{s.get('symbol', 'XAUUSD')} Bot")
    left.caption(f"{s.get('server')} · login {s.get('login')} · {cfg.get('timeframe')} "
                 f"MA{cfg.get('fast')}/{cfg.get('slow')} · SL {cfg.get('sl_atr')}×ATR · ADX ≥ {cfg.get('adx_min')} · "
                 + (f"บล็อกข่าว {cfg.get('news_set')} ±{cfg.get('news_before')} นาที" if cfg.get("news_filter") else "ไม่กรองข่าว"))
    if online:
        right.markdown(f"### :green-badge[● ONLINE]\nอัปเดต {ago(age)} · {th_time(s['updated_at'])} (เวลาไทย)")
    else:
        why = "บอทถูกหยุด" if not s.get("running", False) else "ไม่ได้รับสัญญาณจากบอท (คอมดับ / เน็ตหลุด / โปรแกรมค้าง)"
        right.markdown(f"### :red-badge[● OFFLINE]\n{why} · ล่าสุด {ago(age)}")
    if online and not s.get("algo_trading_on", True):
        st.warning("ปุ่ม Algo Trading ใน MT5 ปิดอยู่ — บอทส่งออเดอร์ไม่ได้")
    if s.get("halted_today"):
        st.error(f"วันนี้ถึงลิมิตขาดทุนรายวันแล้ว (${cfg.get('daily_loss_limit'):g}) — หยุดเข้าไม้ใหม่จนถึงพรุ่งนี้")

    # ---------- ตัวเลขหลัก ----------
    m = st.columns(4)
    floating = s["equity"] - s["balance"]
    m[0].metric(f"Balance ({cur})", f"{s['balance']:,.2f}")
    m[1].metric(f"Equity ({cur})", f"{s['equity']:,.2f}", f"{floating:+,.2f} ลอย" if floating else None)
    limit = cfg.get("daily_loss_limit", 0)
    m[2].metric(f"กำไร/ขาดทุนวันนี้ ({cur})", f"{s['day_pnl']:+,.2f}")
    m[3].metric("ไม้วันนี้", f"{s['entries_today']} / {cfg.get('max_trades_per_day')}")
    st.caption(f"เหลืออีก {limit + s['day_pnl']:,.2f} {cur} ก่อนถึงลิมิตขาดทุนรายวัน (${limit:g}) · "
               f"ราคาล่าสุด Bid {s.get('bid', 0):,.2f} / Ask {s.get('ask', 0):,.2f}")

    # ---------- ไม้ที่เปิด + สิ่งที่บอทรอ + ข่าว ----------
    c1, c2 = st.columns([3, 2])
    with c1:
        st.subheader("ไม้ที่เปิดอยู่")
        if s.get("positions"):
            for p in s["positions"]:
                color = "green" if p["side"] == "BUY" else "red"
                st.markdown(f":{color}-badge[{p['side']}] **{p['volume']} lot** @ {p['price_open']:,.2f} → "
                            f"{p['price_current']:,.2f} · SL {p['sl']:,.2f} · "
                            f"**{p['profit']:+,.2f} {cur}**  \nเปิด {p['open_time']} · เหตุผล: {p['entry_reason'] or '-'}")
        else:
            st.markdown("ไม่มีไม้ที่เปิดอยู่")
        if s.get("armed"):
            st.info(f"รอเข้า **{'BUY' if s['armed'] == 1 else 'SELL'}** — สัญญาณเกิดเมื่อ {s.get('armed_at')} "
                    "แต่ยังติดเงื่อนไข (ADX/ข่าว/ลิมิต) ดูเหตุผลใน Log ด้านล่าง")
        st.caption(f"เวลาเซิร์ฟเวอร์ MT5 {s.get('server_time')} · แท่งล่าสุดที่บอทประมวลผล {s.get('last_bar')}")
    with c2:
        st.subheader("ข่าวสำคัญถัดไป")
        news = s.get("upcoming_news") or []
        if news:
            st.dataframe(pd.DataFrame(news).rename(columns={"time": "เวลาเซิร์ฟเวอร์", "event": "ข่าว"}),
                         hide_index=True, width="stretch")
        else:
            st.markdown("ไม่มีข้อมูลปฏิทินข่าว")

    # ---------- กราฟ equity ----------
    since = now - timedelta(days=days)
    eq = pd.DataFrame(list(db.equity.find({"bot_id": bot_id, "time": {"$gte": since}},
                                          {"_id": 0, "time": 1, "balance": 1, "equity": 1}).sort("time", 1)))
    st.subheader("Balance / Equity")
    if len(eq) > 1:
        eq["time"] = pd.to_datetime(eq["time"], utc=True).dt.tz_convert(TH)
        long = eq.melt("time", ["balance", "equity"], var_name="series", value_name="usd")
        chart = alt.Chart(long).mark_line(strokeWidth=2).encode(
            x=alt.X("time:T", title=None),
            y=alt.Y("usd:Q", title=None, scale=alt.Scale(zero=False)),  # ไม่เริ่มที่ 0 — ไม่งั้นเส้นแบนจนมองไม่เห็นการขยับ
            color=alt.Color("series:N", scale=alt.Scale(domain=["balance", "equity"], range=["#8a919c", "#d4a017"]),
                            legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[alt.Tooltip("time:T", title="เวลาไทย", format="%d/%m %H:%M"), alt.Tooltip("series:N", title=""),
                     alt.Tooltip("usd:Q", title=cur, format=",.2f")],
        ).properties(height=260)
        st.altair_chart(chart, width="stretch")
    else:
        st.caption("กราฟจะเริ่มแสดงหลังบอทรันไปสักพัก (บันทึกทุก 5 นาที)")

    # ---------- ไม้ที่ปิดแล้ว + log ----------
    t1, t2 = st.columns([3, 2])
    with t1:
        st.subheader("ไม้ที่ปิดแล้ว")
        tr = pd.DataFrame(list(db.trades.find({"bot_id": bot_id}, {"_id": 0, "bot_id": 0, "logged_at": 0})
                               .sort("close_time", DESCENDING).limit(100)))
        if tr.empty:
            st.caption("ยังไม่มีไม้ที่ปิด")
        else:
            tr["net_profit"] = pd.to_numeric(tr["net_profit"])
            wins = (tr["net_profit"] > 0).mean() * 100
            st.caption(f"{len(tr)} ไม้ล่าสุด · ชนะ {wins:.0f}% · รวม {tr['net_profit'].sum():+,.2f} {cur}")
            st.dataframe(tr[["close_time", "side", "entry_price", "exit_price", "entry_reason", "exit_reason", "net_profit"]]
                         .rename(columns={"close_time": "ปิดเมื่อ", "side": "ฝั่ง", "entry_price": "เข้า",
                                          "exit_price": "ออก", "entry_reason": "เหตุผลเข้า",
                                          "exit_reason": "เหตุผลออก", "net_profit": "สุทธิ"}),
                         hide_index=True, width="stretch", height=320)
    with t2:
        st.subheader("Log ล่าสุด")
        icon = {"WARNING": "⚠️ ", "ERROR": "⛔ ", "CRITICAL": "⛔ "}
        ev = list(db.events.find({"bot_id": bot_id}).sort("time", DESCENDING).limit(40))
        if ev:
            st.dataframe(pd.DataFrame([{"เวลาไทย": th_time(e["time"]), "เหตุการณ์": icon.get(e["level"], "") + e["msg"]}
                                       for e in ev]), hide_index=True, width="stretch", height=320)
        else:
            st.caption("ยังไม่มี log")


live()
