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
import os
from datetime import datetime, timedelta, timezone

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st
from pymongo import DESCENDING, MongoClient
from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

TH = timezone(timedelta(hours=7))
SERVER_TZ = "Europe/Athens"  # MetaQuotes-Demo: UTC+2/+3 ตาม DST ยุโรป
OFFLINE_AFTER_S = 60  # บอทอัปเดตทุก ~10 วิ ถ้าเงียบเกินนี้ถือว่าหยุด/คอมดับ/เน็ตหลุด
CALENDAR_STALE_MIN = 180
MAX_PIN_TRIES = 5
UP, DOWN, GOLD, MA_FAST, MA_SLOW, MUTED = "#3cc08e", "#f0675c", "#d4a017", "#6b9cff", "#a3aab6", "#8a919c"
ACTIONS = {
    "pause": ("⏸ หยุดเข้าไม้ใหม่", "บอทจะไม่เปิดไม้ใหม่ ไม้ที่ถืออยู่ยังมี SL และออกตามสัญญาณปกติ"),
    "resume": ("▶ กลับมาเข้าไม้ตามปกติ", "บอทกลับมาเปิดไม้ตามสัญญาณ"),
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


def server_to_th(values) -> pd.Series:
    """เวลาเซิร์ฟเวอร์ MT5 (ข้อความ) → เวลาไทยแบบไม่มี timezone (ให้กราฟแสดงตามนั้นตรงๆ)"""
    t = pd.to_datetime(pd.Series(values), errors="coerce")
    t = t.dt.tz_localize(SERVER_TZ, ambiguous="NaT", nonexistent="shift_forward")
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
days = st.sidebar.select_slider("กราฟ equity ย้อนหลัง", options=[1, 7, 30, 90, 365], value=30,
                                format_func=lambda d: f"{d} วัน")
n_bars = st.sidebar.select_slider("กราฟราคา (จำนวนแท่ง)", options=[60, 120, 200, 300], value=120)


# ---------- แผงควบคุม (อยู่นอก auto-refresh เพื่อไม่ให้ PIN ที่พิมพ์อยู่หาย) ----------
def control_panel() -> None:
    sb = st.sidebar
    sb.divider()
    sb.subheader("ควบคุมบอท")
    pin_cfg, ctrl_uri = secret("CONTROL_PIN"), secret("MONGODB_CONTROL_URI")
    if not pin_cfg or not ctrl_uri or "รหัส" in ctrl_uri or "<" in ctrl_uri:
        missing = [n for n, ok in (("MONGODB_CONTROL_URI", ctrl_uri and "รหัส" not in ctrl_uri and "<" not in ctrl_uri),
                                   ("CONTROL_PIN", pin_cfg)) if not ok]
        sb.caption("ปิดอยู่ — ใน Secrets ยังขาด/ยังไม่ได้ใส่รหัสจริง: **" + ", ".join(missing) + "**  \n"
                   "(MONGODB_CONTROL_URI = user ที่เขียนได้เฉพาะ db xauusd_bot, CONTROL_PIN = PIN ที่ตั้งเอง)")
        return
    tries = st.session_state.get("pin_fail", 0)
    if tries >= MAX_PIN_TRIES:
        sb.error("ใส่ PIN ผิดเกินกำหนด — โหลดหน้าใหม่เพื่อลองอีกครั้ง")
        return
    action = sb.radio("คำสั่ง", list(ACTIONS), format_func=lambda a: ACTIONS[a][0], key="ctl_action")
    sb.caption(ACTIONS[action][1])
    pin = sb.text_input("PIN", type="password", key="ctl_pin")
    confirm = sb.checkbox("ยืนยันส่งคำสั่งนี้", key="ctl_confirm")
    if sb.button("ส่งคำสั่ง", type="primary", disabled=not confirm, width="stretch"):
        if not hmac.compare_digest(pin.encode(), pin_cfg.encode()):
            st.session_state.pin_fail = tries + 1
            sb.error(f"PIN ไม่ถูกต้อง ({tries + 1}/{MAX_PIN_TRIES})")
            return
        try:
            get_db("MONGODB_CONTROL_URI").commands.insert_one({
                "bot_id": bot_id, "action": action, "status": "pending",
                "created_at": datetime.now(timezone.utc), "requested_by": "dashboard"})
            sb.success("ส่งคำสั่งแล้ว — บอทจะทำภายใน ~15 วินาที (ดูสถานะด้านล่าง)")
        except PyMongoError as e:
            sb.error(f"ส่งคำสั่งไม่ได้: {type(e).__name__} — เช็คสิทธิ์ของ user ใน MONGODB_CONTROL_URI")
    cmds = list(db.commands.find({"bot_id": bot_id}).sort("created_at", DESCENDING).limit(5))
    if cmds:
        icon = {"pending": "🕓", "received": "⚙️", "done": "✅", "error": "⛔", "expired": "⌛"}
        sb.caption("คำสั่งล่าสุด")
        for c in cmds:
            sb.markdown(f"{icon.get(c['status'], '•')} {th_time(c['created_at'])} **{ACTIONS.get(c['action'], (c['action'],))[0]}**"
                        + (f"  \n<small>{c.get('result', '')}</small>" if c.get("result") else ""), unsafe_allow_html=True)


control_panel()


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
    if not len(sample):
        st.caption("ยังไม่มีข้อมูล backtest ให้เทียบ (รัน backtest.py แล้วรีสตาร์ทบอท)")
        return
    c = st.columns(3)
    c[0].metric("R เฉลี่ยต่อไม้", f"{live.mean():+.2f}R" if n else "–", f"backtest {exp.get('avg_r', 0):+.2f}R",
                delta_color="off", delta_arrow="off")
    c[1].metric("อัตราชนะ", f"{(live > 0).mean() * 100:.0f}%" if n else "–", f"backtest {exp.get('win_rate', 0):.0f}%",
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


@st.fragment(run_every="15s")
def live() -> None:
    s = db.status.find_one({"_id": bot_id})
    now = datetime.now(timezone.utc)
    header(s, now)
    trades = load_trades()
    t1, t2, t3, t4, t5 = st.tabs(["ภาพรวม", "กราฟ", "ข่าว", "ผลงาน", "Log"])
    with t1:
        overview_tab(s, now)
    with t2:
        chart_tab(s, now, trades)
    with t3:
        news_tab(s, now)
    with t4:
        performance_tab(s, trades)
    with t5:
        log_tab()


live()
