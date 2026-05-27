import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import ta
from datetime import datetime
import pytz
import os
import requests
import smtplib
import time as _time
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from streamlit_autorefresh import st_autorefresh

TW_TZ = pytz.timezone("Asia/Taipei")

# ─── 機敏資訊：優先讀 st.secrets，其次環境變數，最後 fallback 空字串 ─────────
def _get_secret(key: str, default: str = "") -> str:
    try:
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)

FUGLE_API_KEY  = _get_secret("FUGLE_API_KEY", "")
FUGLE_BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock"
_FUGLE_TF_MAP  = {"1m": "1", "5m": "5", "15m": "15"}

GMAIL_USER     = _get_secret("GMAIL_USER", "")
GMAIL_PASSWORD = _get_secret("GMAIL_PASSWORD", "")
GMAIL_TO       = _get_secret("GMAIL_TO", GMAIL_USER)
EMAIL_PRIORITY = {"CRITICAL", "HIGH"}
EMAIL_COOLDOWN = 600  # 同一類訊號 Email 冷卻秒數


def _send_email_worker(subject: str, html: str):
    if not (GMAIL_USER and GMAIL_PASSWORD and GMAIL_TO):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = GMAIL_TO
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=12) as srv:
            srv.starttls()
            srv.login(GMAIL_USER, GMAIL_PASSWORD)
            srv.sendmail(GMAIL_USER, GMAIL_TO, msg.as_string())
    except Exception:
        pass


# ── v12 全盤ORB訊號指引 ──────────────────────────────────────────────────────
_ACTION_GUIDE = {
    # 早盤進場
    "開盤突破ORB":      ("🚀 ORB突破做多！",       "09:30-09:35帶量突破ORB高+RSI>55+VWAP乖離≤3%，直接進場"),
    "中盤突破":         ("🚀 中盤突破做多！",       "10:30-11:00 MACD>0+RSI>55+帶量突破ORB高，二次確認進場"),
    "開盤跌破ORB":      ("🔴 ORB跌破沖賣！",       "09:30-09:35帶量跌破ORB低+RSI<40+VWAP乖離<-0.5%，直接沖賣"),
    "中盤跌破":         ("🔴 中盤跌破沖賣！",       "10:30-11:00 MACD<0+RSI<40+帶量跌破ORB低，二次確認沖賣"),
    "ORB回測支撐":      ("🟢 ORB回測再進場！",     "回測ORB高點支撐後再次站上，二次做多機會"),
    "ORB回測壓力":      ("🔴 ORB回測壓力沖賣！",   "反彈到ORB低點壓力後再次跌破，二次沖賣機會"),
    "噴出回測做多":     ("🟢 回測VWAP再起飛！",    "回測不破VWAP/開盤價+拉回≥0.5%+RSI45-68+帶量陽線吞噬"),
    # 午盤進場
    "午盤整理突破":     ("📈 午盤突破！",          "突破11:30-12:30午間整理高點+帶量+RSI回升，下午段進場"),
    "午盤整理沖賣":     ("📉 午盤跌破整理！",      "跌破11:30-12:30午間整理低點+帶量+RSI下行，下午段沖賣"),
    # 全日高品質買點
    "VWAP+RSI買點":     ("💎 高品質全日買點！",    "RSI<30+靠近VWAP(0.3%內)+量比≥1.0x，全天有效"),
    # 停利出場
    "乖離過大先出半":   ("💰 先出一半！",          "噴出BB上軌+RSI>85，先獲利了結一半倉位"),
    "量價背離出清":     ("🚨 全數出清！",          "股價近高點但量縮+RSI背離，追價枯竭，立即全數出場"),
    "動能破壞5MA":      ("⚠️ 動能破壞",           "跌破MA5，做多部位開始減碼"),
    # 停損
    "VWAP跌破停損":     ("🔴 立即停損！",          "明顯跌破VWAP(>0.2%)+帶量，最後防線失守"),
    "空方VWAP停損":     ("🔴 空單停損！",          "明顯站上VWAP(>0.2%)+帶量，空單最後防線失守"),
    "空方乖離停利":     ("💰 空單先回補一半！",     "跌破BB下軌+RSI<20，空方追殺動能耗盡，先回補一半"),
    "空方量價背離":     ("🚨 空單全數回補！",       "股價近低點但量縮+RSI背離，下殺枯竭，立即全數回補"),
    "空方動能回復":     ("⚠️ 空單開始回補",        "站回MA5，空方動能破壞，空單開始減碼"),
    "假突破誘多":       ("🔴 假突破！砍倉",        "突破前高後3棒跌回，誘多陷阱，不猶豫砍倉"),
    "假跌破誘空":       ("🟢 假跌破！回補",        "跌破前低後3棒漲回，誘空陷阱，空單回補"),
    # 警示
    "過熱勿追":         ("🔥 過熱！勿追高",        "RSI>90且離VWAP>3%，等拉回再進場"),
    "縮量開盤警示":     ("⚠️ 量不夠，假動作機率高","開盤量未達前日10%，訊號可信度低"),
    # 出場參考
    "VWAP突破":         ("🔺 空單回補",            "VWAP突破，做空部位回補參考"),
    "MA黃金交叉":       ("🔺 空單回補參考",        "MA5上穿MA10，趨勢轉多"),
    "MA死亡交叉":       ("🔻 多單出場參考",        "MA5下穿MA10，趨勢轉空"),
    "KD黃金交叉":       ("🔺 空單回補參考",        "KD翻多，做空部位回補"),
    "KD死亡交叉":       ("🔻 多單出場參考",        "KD翻空，做多部位減碼"),
    "MACD翻正":         ("🔺 空單回補參考",        "MACD柱轉正，空頭動能減弱"),
    "MACD翻負":         ("🔻 多單出場參考",        "MACD柱轉負，多頭動能減弱"),
    "多方爆量":         ("🔺 多方動能確認",        "爆量在VWAP上方，多頭有量支撐"),
    "空方爆量":         ("🔻 空方動能確認",        "爆量在VWAP下方，注意出貨壓力"),
    "RSI嚴重超買":      ("🚨 緊急出場！",          "RSI嚴重超買，多單立即出場"),
    "RSI嚴重超賣":      ("🚨 緊急回補！",          "RSI嚴重超賣，空單立即回補"),
    "強制出場時間到":   ("⏰ 全部強制出清！",       "13:20已到！嚴禁留倉"),
    "準備出場":         ("⏳ 準備出清部位",         "距13:20不足5分鐘，開始準備出清"),
}


def send_signal_email(sig: dict, ticker: str, price: float):
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    type_color = {"BUY": "#00d48a", "SELL": "#ff6b35", "WARN": "#ffc300"}.get(sig["type"], "#aaa")
    pri_color  = {"CRITICAL": "#ff2222", "HIGH": "#ff8c42", "MEDIUM": "#ffc300"}.get(sig["priority"], "#aaa")
    action_title, action_desc = _ACTION_GUIDE.get(
        sig["category"], ("📌 建議操作", "做多進場/空單回補" if sig["type"] == "BUY" else "做空進場/多單出場"))
    action_color = "#00d48a" if sig["type"] == "BUY" else ("#ff6b35" if sig["type"] == "SELL" else "#ffc300")
    subject = f" 【{action_title[:6]}】{ticker} {sig['icon']} {sig['category']} @ {price:.1f}"
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;background:#0e1117;
                border-radius:14px;padding:24px;color:#fff;border:2px solid {type_color};">
      <h2 style="margin:0 0 6px;color:{type_color}">{sig['icon']} {sig['category']}</h2>
      <p style="font-size:15px;color:#ddd;margin:8px 0 14px">{sig['msg']}</p>
      <div style="background:#1e2d1e;border-left:4px solid {action_color};border-radius:8px;padding:12px 16px;margin:14px 0;">
        <div style="color:{action_color};font-weight:700;font-size:15px;margin-bottom:4px">{action_title}</div>
        <div style="color:#ccc;font-size:13px">{action_desc}</div>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;color:#8892a4;margin-top:10px">
        <tr><td>股票代號</td><td style="color:#fff;text-align:right"><b>{ticker}</b></td></tr>
        <tr><td>現價</td><td style="color:{type_color};text-align:right;font-size:20px"><b>{price:.1f}</b></td></tr>
        <tr><td>優先級</td><td style="color:{pri_color};text-align:right"><b>{sig['priority']}</b></td></tr>
        <tr><td>觸發時間</td><td style="color:#fff;text-align:right">{now_str}</td></tr>
      </table>
      <p style="margin-top:16px;font-size:11px;color:#3a3a5a;border-top:1px solid #2a2a3a;padding-top:10px">
        ⚠️ 本通知僅供技術分析參考，不構成投資建議。當沖風險極高，請謹慎操作。</p>
    </div>"""
    t = threading.Thread(target=_send_email_worker, args=(subject, html), daemon=True)
    t.start()


def send_price_alert_email(ticker: str, current_price: float, alert_price: float, direction: str = "low"):
    """direction: 'low'=現價≤設定值, 'high'=現價≥設定值"""
    now_str   = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    is_low    = direction == "low"
    op_symbol = "≤" if is_low else "≥"
    icon      = "🔴" if is_low else "🟢"
    title     = "跌破限價提醒" if is_low else "突破限價提醒"
    desc      = "現價已達到或低於您設定的下限價，請注意操作。" if is_low else "現價已達到或高於您設定的上限價，請注意操作。"
    price_color = "#ff4b4b" if is_low else "#00d48a"
    border_color= "#ff4b4b" if is_low else "#00d48a"
    subject   = f" 【{icon} {title}】{ticker} 現價 {current_price:.1f} {op_symbol} 設定 {alert_price:.1f}"
    html = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:520px;background:#0e1117;
                border-radius:14px;padding:24px;color:#fff;border:2px solid {border_color};">
      <h2 style="margin:0 0 6px;color:{border_color}">{icon} {title}</h2>
      <p style="font-size:15px;color:#ddd;margin:8px 0 14px">{desc}</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;color:#8892a4;margin-top:10px">
        <tr><td>股票代號</td><td style="color:#fff;text-align:right"><b>{ticker}</b></td></tr>
        <tr><td>現價</td><td style="color:{price_color};text-align:right;font-size:22px"><b>{current_price:.1f}</b></td></tr>
        <tr><td>設定限價（{op_symbol}）</td><td style="color:#ffc300;text-align:right;font-size:18px"><b>{alert_price:.1f}</b></td></tr>
        <tr><td>觸發時間</td><td style="color:#fff;text-align:right">{now_str}</td></tr>
      </table>
      <p style="margin-top:16px;font-size:11px;color:#3a3a5a;border-top:1px solid #2a2a3a;padding-top:10px">
        ⚠️ 本通知僅供技術分析參考，不構成投資建議。當沖風險極高，請謹慎操作。</p>
    </div>"""
    t = threading.Thread(target=_send_email_worker, args=(subject, html), daemon=True)
    t.start()


st.set_page_config(page_title="即時儀表板", page_icon="📈",
                   layout="wide", initial_sidebar_state="expanded")

st.markdown("""
<style>
  body{background:#0e1117}
  .metric-card{background:#1e2130;border-radius:10px;padding:14px 18px;margin:4px 0;border-left:4px solid #4a9eff}
  .metric-label{color:#8892a4;font-size:12px;margin-bottom:4px}
  .metric-value{color:#fff;font-size:22px;font-weight:700}
  .metric-delta{font-size:13px;margin-top:4px}
  .alert-critical{background:linear-gradient(135deg,#2d0808,#4a0f0f);border:2px solid #ff2222;
    border-radius:12px;padding:18px 22px;margin:8px 0;animation:pulse-red 1s infinite}
  .alert-buy{background:linear-gradient(135deg,#082d1a,#0f4a2a);border:2px solid #00d48a;
    border-radius:12px;padding:16px 20px;margin:6px 0}
  .alert-sell{background:linear-gradient(135deg,#2d1208,#4a2008);border:2px solid #ff6b35;
    border-radius:12px;padding:16px 20px;margin:6px 0}
  .alert-warn{background:linear-gradient(135deg,#2d2408,#4a3a08);border:2px solid #ffc300;
    border-radius:12px;padding:16px 20px;margin:6px 0}
  .alert-title{font-size:18px;font-weight:700;margin-bottom:6px}
  .alert-msg{font-size:14px;color:#ddd}
  .alert-time{font-size:11px;color:#888;margin-top:6px}
  @keyframes pulse-red{0%,100%{box-shadow:0 0 10px rgba(255,34,34,.6)}50%{box-shadow:0 0 25px rgba(255,34,34,1)}}
  @keyframes pulse-green{0%,100%{box-shadow:0 0 8px rgba(0,212,138,.4)}50%{box-shadow:0 0 20px rgba(0,212,138,.8)}}
  .status-bar{background:#1e2130;border-radius:8px;padding:8px 16px;display:flex;
    justify-content:space-between;align-items:center;font-size:13px;color:#8892a4;margin-bottom:12px}
  .live-dot{display:inline-block;width:10px;height:10px;background:#00d48a;
    border-radius:50%;animation:blink 1s infinite;margin-right:6px}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
  .log-entry-buy{border-left:3px solid #00d48a;padding:6px 10px;margin:3px 0;
    background:#0a1a12;border-radius:4px;font-size:13px}
  .log-entry-sell{border-left:3px solid #ff6b35;padding:6px 10px;margin:3px 0;
    background:#1a0e08;border-radius:4px;font-size:13px}
  .log-entry-warn{border-left:3px solid #ffc300;padding:6px 10px;margin:3px 0;
    background:#1a1608;border-radius:4px;font-size:13px}
  .log-time{color:#6a7080;font-size:11px}
  .score-bar-bg{background:#2d3446;border-radius:6px;height:12px;margin:6px 0}
  .score-bar-fill{height:12px;border-radius:6px;transition:width .5s ease}
  .range-box{background:#1a1f2e;border-radius:10px;padding:12px 16px;margin:4px 0}
</style>
<script>
function playBeep(f,d){try{var c=new(window.AudioContext||window.webkitAudioContext)();
var o=c.createOscillator();var g=c.createGain();o.connect(g);g.connect(c.destination);
o.frequency.value=f;o.type='sine';g.gain.setValueAtTime(.4,c.currentTime);
g.gain.exponentialRampToValueAtTime(.001,c.currentTime+d);
o.start(c.currentTime);o.stop(c.currentTime+d);}catch(e){}}
</script>
""", unsafe_allow_html=True)


# ─── 資料取得 ────────────────────────────────────────────────────────────────
def _fugle_intraday_candles(symbol: str, interval: str) -> pd.DataFrame:
    if not FUGLE_API_KEY:
        return pd.DataFrame()
    tf = _FUGLE_TF_MAP.get(interval, "5")
    try:
        resp = requests.get(f"{FUGLE_BASE_URL}/intraday/candles/{symbol}",
                            headers={"X-API-KEY": FUGLE_API_KEY},
                            params={"timeframe": tf}, timeout=8)
        if resp.status_code != 200:
            return pd.DataFrame()
        raw_data = resp.json().get("data", [])
        if not raw_data:
            return pd.DataFrame()
        df = pd.DataFrame(raw_data)
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(TW_TZ)
        df = df.set_index("date"); df.index.name = "Datetime"
        df = df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
        return df[["Open","High","Low","Close","Volume"]][df["Close"].notna()].copy()
    except Exception:
        return pd.DataFrame()


def _try_download(symbol: str, period: str, interval: str) -> pd.DataFrame:
    try:
        raw = yf.download(symbol, period=period, interval=interval, auto_adjust=True, progress=False)
    except Exception:
        return pd.DataFrame()
    if raw.empty: return pd.DataFrame()
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    if raw.index.tz is None: raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(TW_TZ)
    return raw[raw["Close"].notna()].copy()


@st.cache_data(ttl=10)
def fetch_and_calc(ticker: str, interval: str, prev_day_vol: int = 0) -> pd.DataFrame:
    """
    取資料 + 技術指標。
    修正(P1)：Vol_ratio 開盤30分鐘內以「前日總量÷270分鐘×當棒分鐘數」估計正常量，避免被前5根爆量綁架。
    """
    period = "5d" if interval in ("1m","2m","5m","15m") else "10d"
    today  = datetime.now(TW_TZ).date()
    fugle_df = _fugle_intraday_candles(ticker, interval)
    fugle_ok = len(fugle_df) >= 5
    resolved_symbol = ticker; data_source = "fugle"

    if fugle_ok:
        df = fugle_df.copy(); used_date = today; is_today = True
    else:
        data_source = "yfinance"; raw = pd.DataFrame()
        for sfx in [".TW", ".TWO"]:
            candidate = _try_download(f"{ticker}{sfx}", period, interval)
            if not candidate.empty:
                raw = candidate; resolved_symbol = f"{ticker}{sfx}"; break
        if raw.empty:
            out = pd.DataFrame()
            out.attrs["error"] = f"富果API查無{ticker}今日資料，Yahoo Finance亦查無。"
            return out
        today_yf = raw[raw.index.date == today]
        is_today  = len(today_yf) >= 5
        if is_today:
            df = today_yf.copy(); used_date = today
        else:
            df = pd.DataFrame(); used_date = None
            for d in sorted({x for x in raw.index.date}, reverse=True):
                c = raw[raw.index.date == d]
                if len(c) >= 5: df = c.copy(); used_date = d; break
        if df.empty or len(df) < 5:
            out = pd.DataFrame(); out.attrs["error"] = f"{ticker}資料不足，可能尚未開盤或非交易日。"
            return out

    c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
    df["VWAP"]    = ((h+l+c)/3*v).cumsum() / v.cumsum()
    df["MA5"]     = c.rolling(5).mean()
    df["MA10"]    = c.rolling(10).mean()
    df["MA20"]    = c.rolling(20).mean()
    bb = ta.volatility.BollingerBands(c, window=20, window_dev=2)
    df["BB_upper"]= bb.bollinger_hband()
    df["BB_lower"]= bb.bollinger_lband()
    df["BB_mid"]  = bb.bollinger_mavg()
    df["RSI6"]    = ta.momentum.RSIIndicator(c, window=6).rsi()
    df["RSI14"]   = ta.momentum.RSIIndicator(c, window=14).rsi()
    stoch = ta.momentum.StochasticOscillator(h, l, c, window=9, smooth_window=3)
    df["K"] = stoch.stoch(); df["D"] = stoch.stoch_signal()
    macd_ind = ta.trend.MACD(c, window_slow=13, window_fast=5, window_sign=5)  # v15: 日內短參數
    df["MACD"]      = macd_ind.macd()
    df["MACD_signal"]= macd_ind.macd_signal()
    df["MACD_hist"] = macd_ind.macd_diff()

    # ── v18 新增：風控與趨勢確認指標 ─────────────────────────────────────────
    # ATR(14)：當沖停損應以波動度動態縮放，固定%對不同波動股不公平
    df["ATR"]     = ta.volatility.AverageTrueRange(h, l, c, window=14).average_true_range()
    df["ATR_pct"] = (df["ATR"] / c * 100).replace([np.inf, -np.inf], np.nan)
    # ADX(14)：趨勢強度。ADX<20 視為盤整，ORB 突破假訊號率最高 → 過濾
    try:
        df["ADX"] = ta.trend.ADXIndicator(h, l, c, window=14).adx()
    except Exception:
        df["ADX"] = np.nan
    # VWAP 斜率：近 3 棒 VWAP 變化%。走揚才做多、走跌才做空，避免走平假突破
    df["VWAP_slope"] = (df["VWAP"] - df["VWAP"].shift(3)) / df["VWAP"].shift(3) * 100

    # ── 量比基準 (P1 修正) ────────────────────────────────────────────────
    # 開盤 30 分內：用「前日總量 / 270 分鐘 × K棒分鐘數」當基準
    # 之後：用 20 根滾動均量（先計算，缺值再 ffill）
    df["Vol_MA5"]  = v.rolling(5).mean()    # 保留供圖表使用
    df["Vol_MA20"] = v.rolling(20).mean()
    bar_minutes = {"1m":1,"5m":5,"15m":15}.get(interval, 5)

    # 單位修正：Fugle 成交量單位是「張」，yfinance 是「股」（1張=1000股）
    # prev_day_vol 來自 yfinance（股）→ 換算成與當前資料相同單位
    vol_unit = 1000 if data_source == "fugle" else 1  # fugle: 1張=1000股
    prev_day_vol_adj = (prev_day_vol // vol_unit) if prev_day_vol > 0 else 0

    if prev_day_vol_adj > 0:
        expected_per_bar = prev_day_vol_adj / 270.0 * bar_minutes
    else:
        expected_per_bar = 0.0
    hhmm = df.index.strftime("%H:%M")
    open_mask = hhmm < "09:30"
    base = df["Vol_MA20"].copy()
    if expected_per_bar > 0:
        # 開盤30分內優先用前日量基準；尚未有效時 fallback 到 Vol_MA5
        base.loc[open_mask] = expected_per_bar
    base = base.fillna(df["Vol_MA5"]).replace(0, np.nan)
    df["Vol_ratio"] = (v / base).replace([np.inf, -np.inf], np.nan).fillna(0)

    for col in ["VWAP","MA5","MA10","RSI6","RSI14","K","D","MACD_hist","BB_upper","BB_lower","BB_mid",
                "ATR","ATR_pct","ADX","VWAP_slope"]:
        df[col] = df[col].ffill()
    df.attrs.update(symbol=resolved_symbol, used_date=str(used_date),
                    is_today=is_today, source=data_source, error="",
                    bar_minutes=bar_minutes,
                    vol_unit=vol_unit, prev_day_vol_adj=prev_day_vol_adj)
    return df


@st.cache_data(ttl=3600)
def get_prev_day_volume(ticker: str) -> int:
    today = datetime.now(TW_TZ).date()
    for sfx in [".TW", ".TWO"]:
        try:
            raw = yf.download(f"{ticker}{sfx}", period="5d", interval="1d",
                              auto_adjust=True, progress=False)
            if raw.empty: continue
            raw.columns = [c[0] if isinstance(c,tuple) else c for c in raw.columns]
            if raw.index.tz is None: raw.index = raw.index.tz_localize("UTC")
            raw.index = raw.index.tz_convert(TW_TZ)
            past = raw[raw.index.date < today]
            if not past.empty: return int(past["Volume"].iloc[-1])
        except Exception: pass
    return 0


@st.cache_data(ttl=3600)
def get_stock_name_tw(ticker: str) -> str:
    if FUGLE_API_KEY:
        try:
            resp = requests.get(f"{FUGLE_BASE_URL}/intraday/quote/{ticker}",
                                headers={"X-API-KEY": FUGLE_API_KEY}, timeout=5)
            if resp.status_code == 200:
                name = resp.json().get("name","")
                if name: return name
        except Exception: pass
    try:
        resp = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=8)
        if resp.status_code == 200:
            for item in resp.json():
                if item.get("Code") == ticker: return item.get("Name","")
    except Exception: pass
    return ""


@st.cache_data(ttl=300)
def get_shares_outstanding(ticker: str) -> int:
    for sfx in [".TW",".TWO"]:
        try:
            info = yf.Ticker(f"{ticker}{sfx}").info
            s = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding") or 0
            if s > 0: return int(s)
        except Exception: pass
    return 0


@st.cache_data(ttl=5)
def get_fugle_bid_ask_vol(ticker: str) -> dict:
    """從 Fugle quote API 取得累計外盤量(tradeVolumeAtAsk) / 內盤量(tradeVolumeAtBid)。"""
    empty = dict(ask_vol=0, bid_vol=0, total_vol=0, ask_pct=0.0, bid_pct=0.0, ok=False)
    if not FUGLE_API_KEY:
        return empty
    try:
        resp = requests.get(f"{FUGLE_BASE_URL}/intraday/quote/{ticker}",
                            headers={"X-API-KEY": FUGLE_API_KEY}, timeout=5)
        if resp.status_code != 200:
            return empty
        total = resp.json().get("total", {})
        ask_vol   = int(total.get("tradeVolumeAtAsk", 0))  # 外盤（主動買）
        bid_vol   = int(total.get("tradeVolumeAtBid", 0))  # 內盤（主動賣）
        total_vol = int(total.get("tradeVolume", 0))
        denom = ask_vol + bid_vol
        ask_pct = ask_vol / denom * 100 if denom > 0 else 0.0
        bid_pct = bid_vol / denom * 100 if denom > 0 else 0.0
        return dict(ask_vol=ask_vol, bid_vol=bid_vol, total_vol=total_vol,
                    ask_pct=ask_pct, bid_pct=bid_pct, ok=denom > 0)
    except Exception:
        return empty


def calc_turnover(df: pd.DataFrame, shares: int, source: str = "yfinance") -> dict:
    if shares <= 0 or df.empty:
        return dict(current_pct=0., current_pct_adj=0., estimated_pct=0.,
                    total_vol=0, total_vol_adj=0, per_bar=pd.Series(dtype=float),
                    correction=1., source=source)
    now_tw   = datetime.now(TW_TZ)
    elapsed  = max(1., min((now_tw - now_tw.replace(hour=9,minute=0,second=0,microsecond=0)).total_seconds()/60, 270.))
    corr     = 1.0 if source=="fugle" else 1.52
    total    = int(df["Volume"].sum())
    total_adj= int(total*corr)
    cur_pct  = total/shares*100
    cur_adj  = total_adj/shares*100
    return dict(current_pct=round(cur_pct,4), current_pct_adj=round(cur_adj,4),
                estimated_pct=round(min(cur_adj*(270/elapsed),100.),4),
                total_vol=total, total_vol_adj=total_adj,
                per_bar=df["Volume"]/shares*100, correction=corr, source=source)


def turnover_label(pct: float) -> tuple:
    if pct<=0:   return "無資料","#8892a4"
    if pct<0.5:  return "冷門","#8892a4"
    if pct<2.0:  return "一般","#ffa500"
    if pct<5.0:  return "熱絡 ✅","#4a9eff"
    if pct<15.0: return "飆股 🔥","#00d48a"
    return "過熱 ⚠️","#ff4b4b"


# ─── 形態 / 評分 ──────────────────────────────────────────────────────────────
def _is_bullish_engulfing(curr, prev) -> bool:
    return (curr["Close"]>curr["Open"] and prev["Close"]<prev["Open"]
            and curr["Open"]<=prev["Close"] and curr["Close"]>=prev["Open"])


def _row_score(row) -> int:
    if pd.isna(row.get("MA10",float("nan"))) or pd.isna(row.get("K",float("nan"))): return 0
    return sum([row["Close"]>row["VWAP"], row["MA5"]>row["MA10"],
                row["K"]>row["D"], row["RSI6"]>50, row["MACD_hist"]>0])


# ─── 開盤區間 (ORB) + 午間整理區間 (NCR) ─────────────────────────────────────
def calc_ranges(df: pd.DataFrame, interval: str = "5m") -> dict:
    """
    ORB: 開盤前 N 分鐘高低點
      - 1m: 09:00-09:14 (15 根)
      - 5m: 09:00-09:29 (6 根，P1 修正：原 3 根代表性過弱)
      - 15m: 09:00-09:29 (2 根)
    NCR: 午間整理 (11:30-12:29) 高低點，12:30後建立完畢
    """
    hm = df.index.strftime("%H:%M")
    # P1 修正：依 interval 拉長 ORB 視窗
    orb_end = {"1m": "09:15", "5m": "09:30", "15m": "09:30"}.get(interval, "09:30")
    orb_min_bars = {"1m": 5, "5m": 2, "15m": 1}.get(interval, 2)

    orb_bars = df[hm < orb_end]
    orb_high = float(orb_bars["High"].max()) if len(orb_bars)>=1 else None
    orb_low  = float(orb_bars["Low"].min())  if len(orb_bars)>=1 else None
    orb_est  = (len(orb_bars) >= orb_min_bars
                and df.index[-1].strftime("%H:%M") >= orb_end)

    open5_bars = df[hm < "09:06"]
    open5_vol  = int(open5_bars["Volume"].sum()) if not open5_bars.empty else 0
    opening_price = float(df.iloc[0]["Open"]) if not df.empty else None

    ncr_mask = (hm>="11:30") & (hm<"12:30")
    ncr_bars = df[ncr_mask]
    ncr_high = float(ncr_bars["High"].max()) if len(ncr_bars)>=2 else None
    ncr_low  = float(ncr_bars["Low"].min())  if len(ncr_bars)>=2 else None
    ncr_est  = len(ncr_bars)>=2 and df.index[-1].strftime("%H:%M")>="12:30"

    return dict(orb_high=orb_high, orb_low=orb_low, orb_established=orb_est,
                orb_end=orb_end, open5_vol=open5_vol, opening_price=opening_price,
                ncr_high=ncr_high, ncr_low=ncr_low, ncr_established=ncr_est)


# ─── 日型判斷（v16 新增）─────────────────────────────────────────────────────
def get_day_character(df: pd.DataFrame) -> tuple:
    """
    v17 修正：09:30-09:35 的 early_bars 只有 1 根，原邏輯回傳 neutral 導致過濾失效。
    修正：early_bars 不足時 fallback 用 ORB 形成段的內部方向判斷（開盤棒方向+最後棒 vs 第一棒）。
    回傳: ("bullish"|"bearish"|"neutral", diff_pct)
    """
    if len(df) < 3:
        return "neutral", 0.0
    hm = df.index.strftime("%H:%M")
    orb_bars   = df[hm < "09:30"]
    early_bars = df[(hm >= "09:30") & (hm < "10:00")]

    if len(orb_bars) < 2:
        return "neutral", 0.0

    orb_mid = (float(orb_bars["High"].max()) + float(orb_bars["Low"].min())) / 2

    if len(early_bars) >= 2:
        # 標準邏輯：09:30-10:00 均價 vs ORB 中線
        early_avg = float(early_bars["Close"].mean())
        diff_pct  = (early_avg - orb_mid) / orb_mid * 100
    else:
        # Fallback（09:30-09:35 時 early_bars 只有 0-1 根）：
        # 用 ORB 形成段的走勢方向判斷——ORB 最後一根收盤 vs 第一根開盤
        first_open = float(orb_bars.iloc[0]["Open"])
        last_close = float(orb_bars.iloc[-1]["Close"])
        diff_pct   = (last_close - first_open) / first_open * 100

    if diff_pct > 0.5:
        return "bullish", round(diff_pct, 2)
    elif diff_pct < -0.5:
        return "bearish", round(diff_pct, 2)
    return "neutral", round(diff_pct, 2)


# ─── 進場條件共用函式 (v15: 加入空方進場條件，與 detect_signals 保持同步) ──
def _check_entry_conditions(df: pd.DataFrame, rng: dict) -> dict:
    """
    v16: 加入日型過濾、ORB突破時間窗口限制、噴出回測強化。
    統計依據（2344 59天回測）：
    - 09:35-10:30進場勝率近0%，ORB突破限縮至09:30-09:35或10:30-11:00+MACD>0
    - 午盤11:35-13:00勝率最高（100%），強化NCR突破
    - 噴出回測要求MACD>0+回測觸及MA10，過濾弱勢訊號
    """
    if len(df) < 5:
        return dict(orb_break=False, orb_retest=False, ncr_break=False,
                    dip_buy=False, vwap_rsi=False,
                    orb_breakdown=False, orb_retest_dn=False, ncr_breakdown=False,
                    entry_type="", entry_direction="", entry_ready=False,
                    long_ready=False, short_ready=False, day_char="neutral")
    curr = df.iloc[-1]; prev = df.iloc[-2]
    bar_hhmm = df.index[-1].strftime("%H:%M")
    orb_high = rng.get("orb_high"); orb_est = rng.get("orb_established", False)
    orb_low  = rng.get("orb_low")
    ncr_high = rng.get("ncr_high"); ncr_est = rng.get("ncr_established", False)
    ncr_low  = rng.get("ncr_low")
    opening_price = rng.get("opening_price")
    _n = len(df)

    vwap_dev = (curr["Close"]-curr["VWAP"])/curr["VWAP"]*100 if curr["VWAP"] else 0

    # v16: 日型判斷（需 >= 10:00 才有足夠資料）
    day_char, day_diff = get_day_character(df)
    macd_ok_long  = not pd.isna(curr.get("MACD_hist", float("nan"))) and curr["MACD_hist"] > 0
    macd_ok_short = not pd.isna(curr.get("MACD_hist", float("nan"))) and curr["MACD_hist"] < 0

    # ── 多方進場 ──────────────────────────────────────────────────────────────
    # v17: 09:30-09:35 才是真正 ORB 突破；10:30-11:00 獨立為「中盤突破」
    orb_break = (orb_est and orb_high and bar_hhmm <= "09:35"
                 and prev["Close"] <= orb_high and curr["Close"] > orb_high
                 and curr["RSI6"] > 55 and 0 <= vwap_dev <= 3.0
                 and curr["Vol_ratio"] >= 1.5
                 and day_char != "bearish")

    mid_break = (orb_est and orb_high and "10:30" <= bar_hhmm <= "11:00"
                 and macd_ok_long
                 and prev["Close"] <= orb_high and curr["Close"] > orb_high
                 and curr["RSI6"] > 55 and 0 <= vwap_dev <= 3.0
                 and curr["Vol_ratio"] >= 1.5
                 and day_char != "bearish")

    had_prior_orb_break_chk = (orb_high and
        any(df.iloc[j]["Close"] > orb_high for j in range(max(0, _n-20), _n-2)))
    orb_retest = (orb_est and orb_high and bar_hhmm >= "09:30"
                  and had_prior_orb_break_chk
                  and prev["Close"] <= orb_high
                  and curr["Close"] > orb_high
                  and curr["RSI6"] > 45 and curr["Vol_ratio"] >= 1.0
                  and curr["Close"] > curr["Open"]
                  and day_char != "bearish")  # v16: 日型過濾

    # v16: 噴出回測強化 — RSI上限62（原68），加MACD>0，回測需觸及MA10
    recent_high = df["High"].tail(8).max()
    pullback_ok = (recent_high - prev["Low"]) / recent_high >= 0.005 if recent_high else False
    ma10_touch  = (not pd.isna(curr.get("MA10", float("nan")))
                   and prev["Low"] <= curr["MA10"] * 1.012)  # 回測需到MA10附近1.2%
    dip_buy = (bar_hhmm >= "09:20" and bar_hhmm < "12:30"
               and curr["Close"] >= curr["VWAP"]
               and (opening_price is None or curr["Close"] >= opening_price*0.997)
               and 45 <= curr["RSI6"] <= 62  # v16: 更嚴格上限，避免高位追入
               and _is_bullish_engulfing(curr, prev)
               and curr["Vol_ratio"] >= 1.0
               and pullback_ok and ma10_touch and macd_ok_long  # v16: 強化條件
               and day_char != "bearish")  # v16: 日型過濾

    ncr_break = (ncr_est and ncr_high and "12:30" <= bar_hhmm <= "13:00"
                 and prev["Close"] <= ncr_high and curr["Close"] > ncr_high
                 and curr["RSI6"] > 50 and curr["Vol_ratio"] >= 1.2  # v16: RSI 55→50 讓更多午盤訊號通過
                 and curr["Close"] > curr["VWAP"])

    vwap_near = abs(curr["Close"]-curr["VWAP"])/curr["VWAP"] <= 0.003 if curr["VWAP"] else False
    _ma_ok_chk = (pd.isna(curr.get("MA5", float("nan"))) or pd.isna(curr.get("MA10", float("nan")))
                  or curr["MA5"] >= curr["MA10"] * 0.995)
    _op_ok_chk = opening_price is None or curr["Close"] >= opening_price * 0.98
    vwap_rsi = (curr["RSI6"] < 30 and vwap_near and curr["Vol_ratio"] >= 1.0
                and _ma_ok_chk and _op_ok_chk
                and day_char != "bearish")  # v16: 超賣買點也過濾強空日

    # ── 空方進場 ──────────────────────────────────────────────────────────────
    # v17: 09:30-09:35 真正 ORB 跌破；10:30-11:00 獨立為「中盤跌破」
    orb_breakdown = (orb_est and orb_low and bar_hhmm <= "09:35"
                     and prev["Close"] >= orb_low and curr["Close"] < orb_low
                     and curr["RSI6"] < 40 and vwap_dev < -0.5
                     and curr["Vol_ratio"] >= 1.5
                     and day_char != "bullish")

    mid_breakdown = (orb_est and orb_low and "10:30" <= bar_hhmm <= "11:00"
                     and macd_ok_short
                     and prev["Close"] >= orb_low and curr["Close"] < orb_low
                     and curr["RSI6"] < 40 and vwap_dev < -0.5
                     and curr["Vol_ratio"] >= 1.5
                     and day_char != "bullish")

    had_prior_orb_break_dn_chk = (orb_low and
        any(df.iloc[j]["Close"] < orb_low for j in range(max(0, _n-20), _n-2)))
    orb_retest_dn = (orb_est and orb_low and bar_hhmm >= "09:30"
                     and had_prior_orb_break_dn_chk
                     and prev["Close"] >= orb_low * 0.997
                     and curr["Close"] < orb_low
                     and curr["RSI6"] < 45 and curr["Vol_ratio"] >= 1.0
                     and curr["Close"] < curr["Open"]
                     and day_char != "bullish")  # v16: 日型過濾

    ncr_breakdown = (ncr_est and ncr_low and "12:30" <= bar_hhmm <= "13:00"
                     and prev["Close"] >= ncr_low and curr["Close"] < ncr_low
                     and curr["RSI6"] < 45 and curr["Vol_ratio"] >= 1.2
                     and curr["Close"] < curr["VWAP"])

    long_ready  = any([orb_break, mid_break, orb_retest, ncr_break, dip_buy, vwap_rsi])
    short_ready = any([orb_breakdown, mid_breakdown, orb_retest_dn, ncr_breakdown])
    entry_ready = long_ready or short_ready

    if   orb_break:      entry_type = "ORB突破";  entry_direction = "做多"
    elif mid_break:      entry_type = "中盤突破";  entry_direction = "做多"
    elif orb_retest:     entry_type = "ORB回測";  entry_direction = "做多"
    elif ncr_break:      entry_type = "NCR突破";  entry_direction = "做多"
    elif dip_buy:        entry_type = "噴出回測"; entry_direction = "做多"
    elif vwap_rsi:       entry_type = "VWAP+RSI"; entry_direction = "做多"
    elif orb_breakdown:  entry_type = "ORB跌破";  entry_direction = "做空"
    elif mid_breakdown:  entry_type = "中盤跌破";  entry_direction = "做空"
    elif orb_retest_dn:  entry_type = "ORB壓力";  entry_direction = "做空"
    elif ncr_breakdown:  entry_type = "NCR跌破";  entry_direction = "做空"
    else:                entry_type = "";          entry_direction = ""

    return dict(orb_break=orb_break, mid_break=mid_break,
                orb_retest=orb_retest, ncr_break=ncr_break,
                dip_buy=dip_buy, vwap_rsi=vwap_rsi,
                orb_breakdown=orb_breakdown, mid_breakdown=mid_breakdown,
                orb_retest_dn=orb_retest_dn, ncr_breakdown=ncr_breakdown,
                entry_type=entry_type, entry_direction=entry_direction,
                entry_ready=entry_ready, long_ready=long_ready, short_ready=short_ready,
                vwap_dev=vwap_dev, pullback_ok=pullback_ok,
                day_char=day_char, day_diff=day_diff)


# ─── Signal Detection v12（全盤ORB版） ───────────────────────────────────────
def detect_signals(df: pd.DataFrame, is_new_bar: bool,
                   rng: dict, prev_day_vol: int) -> list:
    """
    v16 修正版（基於 v15，加入回測統計優化）：
    統計依據：2344 2026/2-5月 59天回測
    v16修正1: ORB突破限縮時間窗口(09:30-09:35 OR 10:30-11:00+MACD>0)，
              消除09:35-10:30死亡陷阱（該時段27筆進場幾乎全損）
    v16修正2: 所有多方訊號加日型過濾 day_char!="bearish"（get_day_character基於早盤vs ORB中線）
    v16修正3: 噴出回測做多加MACD>0+回測觸及MA10(1.2%)，RSI上限62→68，提升確認度
    v16修正4: NCR突破RSI閾值 55→50，讓午盤高勝率(60-100%)訊號更易觸發
    v16修正5: 空方訊號對稱加入日型過濾 day_char!="bullish"
    """
    signals = []
    if len(df) < 5: return signals
    curr = df.iloc[-1]; prev = df.iloc[-2]
    now  = datetime.now(TW_TZ); n = len(df)

    def sig(t, cat, msg, pri, icon):
        return dict(type=t, category=cat, msg=msg, priority=pri, icon=icon)

    orb_high = rng.get("orb_high"); orb_low  = rng.get("orb_low")
    orb_est  = rng.get("orb_established", False)
    opening_price = rng.get("opening_price")
    ncr_high = rng.get("ncr_high"); ncr_low  = rng.get("ncr_low")
    ncr_est  = rng.get("ncr_established", False)

    # 縮量開盤警示
    if (is_new_bar and prev_day_vol>0 and now.hour==9 and 5<=now.minute<=20
            and rng.get("open5_vol",0)>0):
        pct = rng["open5_vol"]/prev_day_vol*100
        if pct < 10:
            signals.append(sig("WARN","縮量開盤警示",
                               f"開盤5分量{rng['open5_vol']:,}股僅佔前日量{pct:.1f}%（需≥10%），"
                               f"主力未進場，當日突破訊號可信度低","HIGH","⚠️"))

    if is_new_bar:
        bar_hhmm = df.index[-1].strftime("%H:%M")
        ind_ok = not (pd.isna(curr.get("RSI6",float("nan"))) or pd.isna(curr.get("MA5",float("nan"))))

        if ind_ok and "09:15" <= bar_hhmm <= "13:20":
            vwap_dev = (curr["Close"]-curr["VWAP"])/curr["VWAP"]*100

            # v16: 日型判斷（全訊號共用）
            day_char, day_diff = get_day_character(df)
            macd_pos = not pd.isna(curr.get("MACD_hist",float("nan"))) and curr["MACD_hist"]>0
            macd_neg = not pd.isna(curr.get("MACD_hist",float("nan"))) and curr["MACD_hist"]<0

            # A. 開盤突破ORB（做多）v17: 限縮至 09:30-09:35
            if (orb_est and orb_high and bar_hhmm <= "09:35"
                    and prev["Close"]<=orb_high and curr["Close"]>orb_high
                    and curr["RSI6"]>55 and 0<=vwap_dev<=3.0 and curr["Vol_ratio"]>=1.5
                    and day_char != "bearish"):
                signals.append(sig("BUY","開盤突破ORB",
                                   f"突破ORB高{orb_high:.1f}，RSI6={curr['RSI6']:.1f}，"
                                   f"VWAP乖離{vwap_dev:.1f}%，量比{curr['Vol_ratio']:.1f}x，"
                                   f"日型:{day_char}，進場！",
                                   "CRITICAL","🚀"))

            # A'. 中盤突破（做多）v17: 10:30-11:00 + MACD>0（獨立訊號）
            if (orb_est and orb_high and "10:30" <= bar_hhmm <= "11:00" and macd_pos
                    and prev["Close"]<=orb_high and curr["Close"]>orb_high
                    and curr["RSI6"]>55 and 0<=vwap_dev<=3.0 and curr["Vol_ratio"]>=1.5
                    and day_char != "bearish"):
                signals.append(sig("BUY","中盤突破",
                                   f"中盤突破ORB高{orb_high:.1f}，MACD正，RSI6={curr['RSI6']:.1f}，"
                                   f"量比{curr['Vol_ratio']:.1f}x，日型:{day_char}，二次確認進場！",
                                   "CRITICAL","🚀"))

            # A. 開盤跌破ORB低（沖賣）v17: 限縮至 09:30-09:35
            if (orb_est and orb_low and bar_hhmm <= "09:35"
                    and prev["Close"]>=orb_low and curr["Close"]<orb_low
                    and curr["RSI6"]<40 and vwap_dev<-0.5 and curr["Vol_ratio"]>=1.5
                    and day_char != "bullish"):
                signals.append(sig("SELL","開盤跌破ORB",
                                   f"跌破ORB低{orb_low:.1f}，RSI6={curr['RSI6']:.1f}，"
                                   f"量比{curr['Vol_ratio']:.1f}x，直接沖賣！",
                                   "CRITICAL","🔴"))

            # A'. 中盤跌破（沖賣）v17: 10:30-11:00 + MACD<0（獨立訊號）
            if (orb_est and orb_low and "10:30" <= bar_hhmm <= "11:00" and macd_neg
                    and prev["Close"]>=orb_low and curr["Close"]<orb_low
                    and curr["RSI6"]<40 and vwap_dev<-0.5 and curr["Vol_ratio"]>=1.5
                    and day_char != "bullish"):
                signals.append(sig("SELL","中盤跌破",
                                   f"中盤跌破ORB低{orb_low:.1f}，MACD負，RSI6={curr['RSI6']:.1f}，"
                                   f"量比{curr['Vol_ratio']:.1f}x，日型:{day_char}，二次確認沖賣！",
                                   "CRITICAL","🔴"))

            # B. ORB回測支撐（做多）v16: 加日型過濾
            had_prior_orb_break = (orb_high and
                any(df.iloc[j]["Close"] > orb_high for j in range(max(0, n-20), n-2)))
            if (orb_est and orb_high and bar_hhmm>="09:30"
                    and had_prior_orb_break
                    and prev["Close"]<=orb_high
                    and curr["Close"]>orb_high
                    and curr["RSI6"]>45 and curr["Vol_ratio"]>=1.0
                    and curr["Close"]>curr["Open"]
                    and day_char != "bearish"):  # v16: 日型過濾
                signals.append(sig("BUY","ORB回測支撐",
                                   f"回測ORB高{orb_high:.1f}支撐後再度站上，"
                                   f"RSI6={curr['RSI6']:.1f}，量比{curr['Vol_ratio']:.1f}x，"
                                   f"日型:{day_char}，二次做多機會！","HIGH","🟢"))

            # B. ORB回測壓力（沖賣）v16: 加日型過濾
            had_prior_orb_break_dn = (orb_low and
                any(df.iloc[j]["Close"] < orb_low for j in range(max(0, n-20), n-2)))
            if (orb_est and orb_low and bar_hhmm>="09:30"
                    and had_prior_orb_break_dn
                    and prev["Close"]>=orb_low*0.997
                    and curr["Close"]<orb_low
                    and curr["RSI6"]<45 and curr["Vol_ratio"]>=1.0
                    and curr["Close"]<curr["Open"]
                    and day_char != "bullish"):  # v16: 日型過濾
                signals.append(sig("SELL","ORB回測壓力",
                                   f"反彈至ORB低{orb_low:.1f}壓力後再跌破，"
                                   f"RSI6={curr['RSI6']:.1f}，量比{curr['Vol_ratio']:.1f}x，"
                                   f"日型:{day_char}，二次沖賣機會！","HIGH","🔴"))

            # C. 噴出回測做多 v16: RSI上限62，加MACD>0+MA10觸及+日型過濾
            above_vwap = curr["Close"]>=curr["VWAP"]
            above_open = opening_price and curr["Close"]>=opening_price*0.997
            rsi_reset  = 45<=curr["RSI6"]<=62  # v16: 更嚴格上限
            engulf     = _is_bullish_engulfing(curr, prev)
            recent_high = df["High"].tail(8).max()
            pullback_pct = (recent_high - prev["Low"]) / recent_high if recent_high else 0
            ma10_touch = (not pd.isna(curr.get("MA10",float("nan")))
                          and prev["Low"] <= curr["MA10"] * 1.012)  # 回測需觸及MA10 1.2%內
            if (bar_hhmm>="09:20" and above_vwap and above_open
                    and rsi_reset and engulf and curr["Vol_ratio"]>=1.0
                    and pullback_pct >= 0.005 and ma10_touch and macd_pos  # v16: 強化條件
                    and bar_hhmm<"12:30"
                    and day_char != "bearish"):  # v16: 日型過濾
                signals.append(sig("BUY","噴出回測做多",
                                   f"回測至MA10{curr['MA10']:.1f}附近，拉回{pullback_pct*100:.2f}%，"
                                   f"RSI6={curr['RSI6']:.1f}（45-62），MACD正，"
                                   f"日型:{day_char}，帶量{curr['Vol_ratio']:.1f}x陽線吞噬！",
                                   "HIGH","🟢"))

            # D. 午盤整理突破 v16: RSI 55→50，讓高勝率午盤訊號更易觸發
            if ncr_est and "12:30" <= bar_hhmm <= "13:00":
                if (ncr_high and prev["Close"]<=ncr_high
                        and curr["Close"]>ncr_high
                        and curr["RSI6"]>50 and curr["Vol_ratio"]>=1.2  # v16: 50（原55）
                        and curr["Close"]>curr["VWAP"]):
                    signals.append(sig("BUY","午盤整理突破",
                                       f"突破午間整理高{ncr_high:.1f}，"
                                       f"RSI6={curr['RSI6']:.1f}，量比{curr['Vol_ratio']:.1f}x，"
                                       f"VWAP上方，午盤黃金時段做多！","HIGH","📈"))
                if (ncr_low and prev["Close"]>=ncr_low
                        and curr["Close"]<ncr_low
                        and curr["RSI6"]<45 and curr["Vol_ratio"]>=1.2
                        and curr["Close"]<curr["VWAP"]):  # v17: 同步 _check_entry_conditions
                    signals.append(sig("SELL","午盤整理沖賣",
                                       f"跌破午間整理低{ncr_low:.1f}，"
                                       f"RSI6={curr['RSI6']:.1f}，量比{curr['Vol_ratio']:.1f}x，"
                                       f"VWAP下方，午盤段沖賣！","HIGH","📉"))

            # E. VWAP+RSI 全日高品質買點 v16: 加日型過濾
            vwap_near = abs(curr["Close"]-curr["VWAP"])/curr["VWAP"]<=0.003
            _op_ok = opening_price is None or curr["Close"] >= opening_price * 0.98
            _ma_ok = (pd.isna(curr.get("MA5", float("nan"))) or pd.isna(curr.get("MA10", float("nan")))
                      or curr["MA5"] >= curr["MA10"] * 0.995)
            if (curr["RSI6"]<30 and vwap_near and curr["Vol_ratio"]>=1.0
                    and _op_ok and _ma_ok and day_char != "bearish"):  # v16: 空方日不接刀
                signals.append(sig("BUY","VWAP+RSI買點",
                                   f"RSI6={curr['RSI6']:.1f}超賣，靠近VWAP{curr['VWAP']:.1f}（差{abs(curr['Close']-curr['VWAP']):.2f}），"
                                   f"量比{curr['Vol_ratio']:.1f}x，日型:{day_char}，全日高品質買點",
                                   "HIGH","💎"))

            # F. 量價背離出清
            lb = min(n-1, 15)
            if lb>=5:
                past_c = [df.iloc[-(j+1)]["Close"] for j in range(1,lb+1)]
                mx_c   = max(past_c); idx_mx = past_c.index(mx_c)
                vol_mx = df.iloc[-(idx_mx+2)]["Vol_ratio"]
                rsi_mx = df.iloc[-(idx_mx+2)]["RSI6"]
                if (curr["Close"]>=mx_c*0.997 and curr["Vol_ratio"]<vol_mx*0.75
                        and curr["RSI6"]<rsi_mx-5 and curr["Close"]>curr["VWAP"]):
                    signals.append(sig("SELL","量價背離出清",
                                       f"股價{curr['Close']:.1f}近高點{mx_c:.1f}，"
                                       f"量比{curr['Vol_ratio']:.1f}x<前高{vol_mx:.1f}x，"
                                       f"RSI6={curr['RSI6']:.1f}<前高{rsi_mx:.1f}，全數出清！",
                                       "CRITICAL","🚨"))

            # G. 乖離過大先出半
            if curr["Close"]>curr["BB_upper"] and curr["RSI6"]>85 and curr["Vol_ratio"]>=0.7:
                bb_dev = (curr["Close"]-curr["BB_upper"])/curr["BB_upper"]*100
                signals.append(sig("SELL","乖離過大先出半",
                                   f"噴出BB上軌{curr['BB_upper']:.1f}外{bb_dev:.1f}%，"
                                   f"RSI6={curr['RSI6']:.1f}極度超買，先出一半倉位",
                                   "HIGH","💰"))

            # H. 動能破壞 5MA
            if prev["Close"]>prev["MA5"] and curr["Close"]<curr["MA5"] and curr["Vol_ratio"]>=0.7:
                signals.append(sig("SELL","動能破壞5MA",
                                   f"跌破MA5{curr['MA5']:.1f}，慣性破壞，做多部位減碼",
                                   "MEDIUM","⚠️"))

            # J. 假突破誘多（ORB高）── P0 重大修正 ──
            # 必要條件：(1) 前棒在 ORB 高之上 (2) 當棒明顯跌回 ORB 高 0.3% 以下
            # (3) 過去 4 棒內曾有「連續站上 ORB 高 ≥2 棒」的真實突破紀錄 (4) 帶量
            if (orb_high and bar_hhmm <= "11:30"
                    and prev["Close"] > orb_high
                    and curr["Close"] < orb_high * 0.997
                    and curr["Vol_ratio"] >= 1.0):
                lb_fb = min(n-2, 5)
                # 看過去 5 棒（不含當棒、前棒）裡是否有連續 ≥2 棒站上 ORB 高
                consec = 0; had_real_break = False
                for j in range(2, lb_fb+2):
                    if df.iloc[-(j+1)]["Close"] > orb_high:
                        consec += 1
                        if consec >= 2: had_real_break = True; break
                    else:
                        consec = 0
                if had_real_break:
                    signals.append(sig("SELL","假突破誘多",
                                       f"突破ORB高{orb_high:.1f}後明顯跌回（現價{curr['Close']:.1f}），"
                                       f"量比{curr['Vol_ratio']:.1f}x，誘多陷阱！砍倉",
                                       "CRITICAL","🔴"))

            # J. 假跌破誘空（ORB低）── P0 重大修正：對稱處理 ──
            if (orb_low and bar_hhmm <= "11:30"
                    and prev["Close"] < orb_low
                    and curr["Close"] > orb_low * 1.003
                    and curr["Vol_ratio"] >= 1.0):
                lb_fb = min(n-2, 5)
                consec = 0; had_real_break = False
                for j in range(2, lb_fb+2):
                    if df.iloc[-(j+1)]["Close"] < orb_low:
                        consec += 1
                        if consec >= 2: had_real_break = True; break
                    else:
                        consec = 0
                if had_real_break:
                    signals.append(sig("BUY","假跌破誘空",
                                       f"跌破ORB低{orb_low:.1f}後明顯漲回（現價{curr['Close']:.1f}），"
                                       f"量比{curr['Vol_ratio']:.1f}x，誘空陷阱！回補",
                                       "CRITICAL","🟢"))

            # K. 過熱勿追
            if curr["RSI6"]>90 and vwap_dev>3.0:
                signals.append(sig("WARN","過熱勿追",
                                   f"RSI6={curr['RSI6']:.1f}極熱+VWAP乖離{vwap_dev:.1f}%>3%，等拉回",
                                   "HIGH","🔥"))

            # 標準出場參考（v17: 移除冗餘的KD/MA/MACD交叉，當沖太慢 → 保留VWAP突破）
            vol_ok = curr["Vol_ratio"]>=0.7
            if vol_ok:
                if prev["Close"]<prev["VWAP"] and curr["Close"]>curr["VWAP"]:
                    signals.append(sig("BUY","VWAP突破",
                                       f"現價{curr['Close']:.1f}突破VWAP{curr['VWAP']:.1f}",
                                       "HIGH","🚀"))

        # 爆量
        if ind_ok and curr["Vol_ratio"]>=2.0:
            cat = "多方爆量" if curr["Close"]>curr["VWAP"] else "空方爆量"
            signals.append(sig("BUY" if curr["Close"]>curr["VWAP"] else "SELL", cat,
                               f"量比{curr['Vol_ratio']:.1f}x在VWAP{'上' if curr['Close']>curr['VWAP'] else '下'}方",
                               "MEDIUM","💥"))

    # ── 持續性訊號 ────────────────────────────────────────────────────────────
    # I. VWAP 跌破停損 + 空方出場訊號 (v17: 加入對稱空方出場)
    if is_new_bar:
        if (not pd.isna(curr.get("VWAP",float("nan")))
                and prev["Close"] > prev["VWAP"]
                and curr["Close"] < curr["VWAP"] * 0.998   # 明顯跌破
                and curr["Vol_ratio"] >= 0.8):              # 非縮量假摔
            dev_pct = (curr["VWAP"] - curr["Close"]) / curr["VWAP"] * 100
            signals.append(sig("SELL","VWAP跌破停損",
                               f"現價{curr['Close']:.1f}明顯跌破VWAP{curr['VWAP']:.1f}（-{dev_pct:.2f}%）"
                               f"+量比{curr['Vol_ratio']:.1f}x，最後防線失守！立即停損",
                               "CRITICAL","🔴"))

        # 空方 VWAP 停損（v17 新增）：空單持有時股價站回VWAP上方
        if (not pd.isna(curr.get("VWAP",float("nan")))
                and prev["Close"] < prev["VWAP"]
                and curr["Close"] > curr["VWAP"] * 1.002   # 明顯站上
                and curr["Vol_ratio"] >= 0.8):
            dev_pct = (curr["Close"] - curr["VWAP"]) / curr["VWAP"] * 100
            signals.append(sig("BUY","空方VWAP停損",
                               f"現價{curr['Close']:.1f}明顯站上VWAP{curr['VWAP']:.1f}（+{dev_pct:.2f}%）"
                               f"+量比{curr['Vol_ratio']:.1f}x，空單最後防線失守！立即停損",
                               "CRITICAL","🔴"))

        # 空方乖離停利（v17 新增）：跌破BB下軌+RSI<20，追殺動能耗盡
        if (not pd.isna(curr.get("BB_lower",float("nan")))
                and curr["Close"] < curr["BB_lower"] and curr["RSI6"] < 20):
            signals.append(sig("BUY","空方乖離停利",
                               f"跌破BB下軌{curr['BB_lower']:.1f}，RSI6={curr['RSI6']:.1f}<20，"
                               f"空方追殺動能耗盡，先回補一半",
                               "HIGH","💰"))

        # 空方量價背離（v17 新增）：股價近低點但量縮+RSI反彈，下殺枯竭
        mn = min(n-1, 15)
        if mn >= 5:
            past_c2   = [df.iloc[-(j+1)]["Close"] for j in range(1, mn+1)]
            mn_c      = min(past_c2); idx_mn = past_c2.index(mn_c)
            vol_mn    = df.iloc[-(idx_mn+2)]["Vol_ratio"]
            rsi_mn    = df.iloc[-(idx_mn+2)]["RSI6"]
            if (curr["Close"] <= mn_c*1.003 and curr["Vol_ratio"] < vol_mn*0.75
                    and curr["RSI6"] > rsi_mn+5 and curr["Close"] < curr["VWAP"]):
                signals.append(sig("BUY","空方量價背離",
                                   f"股價{curr['Close']:.1f}近低點{mn_c:.1f}，"
                                   f"量比{curr['Vol_ratio']:.1f}x<前低{vol_mn:.1f}x，"
                                   f"RSI6={curr['RSI6']:.1f}>前低{rsi_mn:.1f}，下殺枯竭，全數回補！",
                                   "CRITICAL","🚨"))

        # 空方動能回復（v17 新增）：站回MA5，空方動能破壞
        if (not pd.isna(curr.get("MA5",float("nan")))
                and prev["Close"] < prev["MA5"] and curr["Close"] > curr["MA5"]
                and curr["Vol_ratio"] >= 0.7):
            signals.append(sig("BUY","空方動能回復",
                               f"站回MA5{curr['MA5']:.1f}，空方動能破壞，空單開始減碼",
                               "MEDIUM","⚠️"))

        # RSI 嚴重超買/超賣 v14: 超買門檻從88提高到92，避免強勢股過早被踢出
        if curr["RSI6"]>92:
            signals.append(sig("SELL","RSI嚴重超買",f"RSI6={curr['RSI6']:.1f}，多單立即出場","CRITICAL","🚨"))
        elif curr["RSI6"]<17:
            signals.append(sig("BUY","RSI嚴重超賣",f"RSI6={curr['RSI6']:.1f}，空單立即回補","CRITICAL","🚨"))

    # v18 修正：強制出場「不再」綁定 is_new_bar。
    # 原因：5m K 棒換棒不一定落在 13:20-13:21，原邏輯會整個漏掉最關鍵的強制平倉訊號。
    # 改為時間到就觸發（每次刷新檢查），下游 level_cooldown(60s) 已防止洗版。
    _hm = now.strftime("%H:%M")
    if "13:20" <= _hm <= "13:30":
        signals.append(sig("SELL","強制出場時間到",
                           f"現在{_hm}！已過13:20，所有當沖部位必須立即無條件出清！","CRITICAL","⏰"))
    elif "13:13" <= _hm < "13:20":
        _left = (13*60+20) - (now.hour*60+now.minute)
        signals.append(sig("WARN","準備出場",
                           f"距13:20強制出場剩 {_left} 分鐘，開始分批出清部位","HIGH","⏳"))

    # P2 修正：訊號去重與優先級排序
    # 1) 去重：同 category 只保留一筆
    seen = set(); deduped = []
    for s in signals:
        if s["category"] not in seen:
            deduped.append(s); seen.add(s["category"])
    # 2) 優先級排序：CRITICAL > HIGH > MEDIUM；同優先級內 SELL > BUY > WARN（風控優先）
    pri_rank  = {"CRITICAL":0, "HIGH":1, "MEDIUM":2}
    type_rank = {"SELL":0, "WARN":1, "BUY":2}
    deduped.sort(key=lambda s: (pri_rank.get(s["priority"],9), type_rank.get(s["type"],9)))
    return deduped


# ─── v18 趨勢確認過濾器（單一注入點，不動原雙份條件邏輯） ────────────────────
# 統計事實：ORB/突破型策略在「盤整日」(ADX 低、VWAP 走平) 假訊號率最高，
# 是當沖長期失血主因。此過濾器只「減訊號」不「加訊號」——
# 當趨勢未確認時，抑制「進場型」突破訊號；風控/停損/出場訊號一律放行。
_ENTRY_CATS_LONG  = {"開盤突破ORB","中盤突破","ORB回測支撐","噴出回測做多",
                     "午盤整理突破","VWAP+RSI買點"}
_ENTRY_CATS_SHORT = {"開盤跌破ORB","中盤跌破","ORB回測壓力","午盤整理沖賣"}

def apply_trend_confirmation(signals: list, df: pd.DataFrame,
                             enabled: bool, adx_min: float = 20.0) -> tuple:
    """回傳 (放行後的 signals, 被擋下的說明 list)。停損/出場/警示永不被擋。"""
    if not enabled or df.empty:
        return signals, []
    curr = df.iloc[-1]
    adx   = curr.get("ADX", float("nan"))
    slope = curr.get("VWAP_slope", float("nan"))
    kept, blocked = [], []
    for s in signals:
        cat = s["category"]
        is_long_entry  = cat in _ENTRY_CATS_LONG
        is_short_entry = cat in _ENTRY_CATS_SHORT
        if not (is_long_entry or is_short_entry):
            kept.append(s); continue          # 非進場型（含所有停損/出場）→ 放行
        # 趨勢強度不足（盤整）→ 擋
        if not pd.isna(adx) and adx < adx_min:
            blocked.append(f"{s['icon']}{cat}（ADX={adx:.0f}<{adx_min:.0f} 盤整，假突破機率高）")
            continue
        # VWAP 斜率與方向相反 → 擋
        if not pd.isna(slope):
            if is_long_entry and slope < -0.03:
                blocked.append(f"{s['icon']}{cat}（VWAP走跌{slope:.2f}%，與做多相悖）")
                continue
            if is_short_entry and slope > 0.03:
                blocked.append(f"{s['icon']}{cat}（VWAP走揚{slope:.2f}%，與做空相悖）")
                continue
        kept.append(s)
    return kept, blocked


def get_composite_signal(df: pd.DataFrame, rng: dict) -> dict:
    """v15: 使用 _check_entry_conditions，同時支援做多/做空方向"""
    if df.empty or len(df)<5:
        return dict(direction="無資料",score=0,color="#8892a4",
                    entry_ready=False,entry_dir="",entry_type="",
                    long_ready=False,short_ready=False)
    r = df.iloc[-1]
    score = _row_score(r)
    chk = _check_entry_conditions(df, rng)
    entry_ready = chk["entry_ready"]
    entry_dir   = chk.get("entry_direction", "")  # v15: 包含做空方向

    colors = {4:"#00d48a",3:"#4a9eff",2:"#ffa500",1:"#ff8c42",0:"#ff4b4b"}
    dirs   = {4:"強烈做多",3:"偏多觀望",2:"中立觀望",1:"偏空觀望",0:"強烈做空"}
    sc = max(0,min(score,4))
    return dict(direction=dirs[sc], score=score, color=colors[sc],
                entry_ready=entry_ready, entry_dir=entry_dir,
                entry_type=chk["entry_type"],
                long_ready=chk.get("long_ready",False),
                short_ready=chk.get("short_ready",False))


def risk_calc(entry, direction, capital, risk_pct,
              mode="fixed", atr=0.0, atr_mult=1.5):
    """
    v18: 停損支援兩種模式
      - fixed: 固定 risk_pct%（原邏輯）
      - atr  : 停損距離 = atr_mult × ATR（波動度自適應，當沖正解）
    兩種模式下，部位大小皆以「最大虧損 = 資金 × risk_pct%」反推，
    確保每筆風險金額一致，只是停損點位依波動度浮動。
    """
    if mode == "atr" and atr and atr > 0:
        stop_dist = atr_mult * atr
    else:
        stop_dist = entry * risk_pct / 100.0
    stop_dist = max(stop_dist, entry * 0.001)  # 防呆：至少 0.1%
    if direction == "做多":
        stop = entry - stop_dist
        tp1  = entry + stop_dist * 2
        tp2  = entry + stop_dist * 3
    else:
        stop = entry + stop_dist
        tp1  = entry - stop_dist * 2
        tp2  = entry - stop_dist * 3
    max_loss = capital * risk_pct / 100.0
    shares   = max(1000, int(max_loss / stop_dist / 1000) * 1000)
    return dict(stop=stop, tp1=tp1, tp2=tp2, shares=shares,
                cost=entry*shares*1.001425, max_loss=max_loss,
                fee=entry*shares*0.00435,
                stop_dist=stop_dist, mode=mode, atr=atr, atr_mult=atr_mult)


# ─── Chart ──────────────────────────────────────────────────────────────────
def make_chart(df, ticker, signals_log, rng):
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True,
                        row_heights=[0.46,0.17,0.18,0.19], vertical_spacing=0.02,
                        subplot_titles=("K線+VWAP+均線+ORB+NCR","成交量","RSI(6/14)","KD+MACD"))

    fig.add_trace(go.Candlestick(x=df.index, open=df["Open"], high=df["High"],
                                 low=df["Low"], close=df["Close"], name="K線",
                                 increasing_line_color="#00d48a", decreasing_line_color="#ff4b4b"), row=1,col=1)

    for col,color,name,w,dash in [
        ("VWAP","#f0e040","VWAP",1.8,"solid"),("MA5","#4a9eff","MA5",1.5,"solid"),
        ("MA10","#ff8c42","MA10",1.2,"solid"),("BB_upper","#9b59b6","BB上軌",1.0,"dash"),
        ("BB_lower","#9b59b6","BB下軌",1.0,"dash"),("BB_mid","#6c5ce7","BB中線",0.8,"dot")]:
        fig.add_trace(go.Scatter(x=df.index,y=df[col],name=name,
                                 line=dict(color=color,width=w,dash=dash)), row=1,col=1)

    # ── ORB / NCR 時間帶 + 區間 ────────────────────────────────────────────────
    if not df.empty:
        day0 = df.index[0]

        # ORB 形成時間帶（縱向淡綠填色）
        orb_end_str = rng.get("orb_end", "09:30")
        xs_orb = day0.replace(hour=9, minute=0, second=0)
        xe_orb = day0.replace(hour=int(orb_end_str[:2]), minute=int(orb_end_str[3:]), second=0)
        fig.add_vrect(x0=xs_orb, x1=xe_orb,
                      fillcolor="rgba(0,255,136,0.06)", line_width=0,
                      annotation_text="ORB形成期",
                      annotation_position="top left",
                      annotation_font_color="#00ff88",
                      annotation_font_size=10,
                      row=1, col=1)

        # NCR 形成時間帶（縱向淡橙填色）
        xs_ncr = day0.replace(hour=11, minute=30, second=0)
        xe_ncr = day0.replace(hour=12, minute=30, second=0)
        fig.add_vrect(x0=xs_ncr, x1=xe_ncr,
                      fillcolor="rgba(255,165,0,0.06)", line_width=0,
                      annotation_text="NCR形成期",
                      annotation_position="top left",
                      annotation_font_color="#ffa500",
                      annotation_font_size=10,
                      row=1, col=1)

    # ORB 高低點之間填色
    if rng.get("orb_high") and rng.get("orb_low"):
        fig.add_hrect(y0=rng["orb_low"], y1=rng["orb_high"],
                      fillcolor="rgba(0,255,136,0.04)", line_width=0, row=1, col=1)

    # ORB 高線（粗綠虛線 + 有色框 annotation）
    if rng.get("orb_high"):
        fig.add_hline(y=rng["orb_high"], line_dash="dash", line_color="#00ff88", line_width=2.5,
                      annotation_text=f"▌ORB高 {rng['orb_high']:.1f}",
                      annotation_position="right",
                      annotation_bgcolor="#0d2a1a",
                      annotation_bordercolor="#00ff88",
                      annotation_borderwidth=1,
                      annotation_borderpad=4,
                      annotation_font_color="#00ff88",
                      annotation_font_size=12,
                      row=1, col=1)

    # ORB 低線（粗紅虛線 + 有色框 annotation）
    if rng.get("orb_low"):
        fig.add_hline(y=rng["orb_low"], line_dash="dash", line_color="#ff4b4b", line_width=2.5,
                      annotation_text=f"▌ORB低 {rng['orb_low']:.1f}",
                      annotation_position="right",
                      annotation_bgcolor="#2a0d0d",
                      annotation_bordercolor="#ff4b4b",
                      annotation_borderwidth=1,
                      annotation_borderpad=4,
                      annotation_font_color="#ff4b4b",
                      annotation_font_size=12,
                      row=1, col=1)

    # 開盤價線（黃點線）
    if rng.get("opening_price"):
        fig.add_hline(y=rng["opening_price"], line_dash="dot", line_color="#f0e040", line_width=1.2,
                      annotation_text=f"開盤 {rng['opening_price']:.1f}",
                      annotation_position="right",
                      annotation_font_color="#f0e040",
                      annotation_font_size=11,
                      row=1, col=1)

    # NCR 高低點之間填色（較深橙）
    if rng.get("ncr_high") and rng.get("ncr_low"):
        fig.add_hrect(y0=rng["ncr_low"], y1=rng["ncr_high"],
                      fillcolor="rgba(255,165,0,0.10)", line_width=0, row=1, col=1)

    # NCR 高線（橙色點劃線 + 有色框）
    if rng.get("ncr_high"):
        fig.add_hline(y=rng["ncr_high"], line_dash="dashdot", line_color="#ffa500", line_width=2,
                      annotation_text=f"▌NCR高 {rng['ncr_high']:.1f}",
                      annotation_position="right",
                      annotation_bgcolor="#2a1800",
                      annotation_bordercolor="#ffa500",
                      annotation_borderwidth=1,
                      annotation_borderpad=4,
                      annotation_font_color="#ffa500",
                      annotation_font_size=12,
                      row=1, col=1)

    # NCR 低線（深橙點劃線 + 有色框）
    if rng.get("ncr_low"):
        fig.add_hline(y=rng["ncr_low"], line_dash="dashdot", line_color="#ff8c00", line_width=2,
                      annotation_text=f"▌NCR低 {rng['ncr_low']:.1f}",
                      annotation_position="right",
                      annotation_bgcolor="#2a1800",
                      annotation_bordercolor="#ff8c00",
                      annotation_borderwidth=1,
                      annotation_borderpad=4,
                      annotation_font_color="#ff8c00",
                      annotation_font_size=12,
                      row=1, col=1)

    for entry in signals_log[:20]:
        try:
            t = TW_TZ.localize(datetime.strptime(
                datetime.now(TW_TZ).strftime("%Y-%m-%d")+" "+entry["time"], "%Y-%m-%d %H:%M:%S"))
            ca = df["Close"].asof(t)
            if ca and not np.isnan(ca):
                color = "#00d48a" if entry["type"]=="BUY" else ("#ff4b4b" if entry["type"]=="SELL" else "#ffc300")
                fig.add_annotation(x=t, y=ca, text=entry["icon"], showarrow=True, arrowhead=2,
                                   arrowcolor=color, arrowsize=1.2, arrowwidth=2,
                                   ax=0, ay=-30, font=dict(size=14), row=1,col=1)
        except Exception: pass

    bar_colors=["#00d48a" if c>=o else "#ff4b4b" for c,o in zip(df["Close"],df["Open"])]
    fig.add_trace(go.Bar(x=df.index,y=df["Volume"],name="成交量",marker_color=bar_colors,opacity=0.7),row=2,col=1)
    fig.add_trace(go.Scatter(x=df.index,y=df["Vol_MA5"],name="量MA5",line=dict(color="#ffa500",width=1.5)),row=2,col=1)

    fig.add_trace(go.Scatter(x=df.index,y=df["RSI6"],name="RSI6",line=dict(color="#4a9eff",width=1.8)),row=3,col=1)
    fig.add_trace(go.Scatter(x=df.index,y=df["RSI14"],name="RSI14",line=dict(color="#ff8c42",width=1.2,dash="dash")),row=3,col=1)
    # v15: 加 92/17 實際訊號觸發線（與 RSI嚴重超買/超賣 閾值對齊）
    for lvl,clr in [(92,"rgba(255,34,34,.8)"),(85,"rgba(255,75,75,.5)"),
                    (70,"rgba(255,140,66,.4)"),(50,"rgba(255,255,255,.2)"),
                    (30,"rgba(74,158,255,.4)"),(17,"rgba(0,212,138,.8)")]:
        fig.add_hline(y=lvl,line_dash="dot",line_color=clr,row=3,col=1)
    fig.add_hrect(y0=92,y1=100,fillcolor="rgba(255,34,34,.08)",line_width=0,row=3,col=1)
    fig.add_hrect(y0=85,y1=92,fillcolor="rgba(255,75,75,.04)",line_width=0,row=3,col=1)
    fig.add_hrect(y0=0,y1=17,fillcolor="rgba(0,212,138,.08)",line_width=0,row=3,col=1)
    fig.add_hrect(y0=17,y1=30,fillcolor="rgba(0,212,138,.04)",line_width=0,row=3,col=1)

    fig.add_trace(go.Scatter(x=df.index,y=df["K"],name="K值",line=dict(color="#00d48a",width=1.8)),row=4,col=1)
    fig.add_trace(go.Scatter(x=df.index,y=df["D"],name="D值",line=dict(color="#ff4b4b",width=1.8)),row=4,col=1)
    hc=["rgba(0,212,138,.7)" if v>=0 else "rgba(255,75,75,.7)" for v in df["MACD_hist"]]
    ms=30/(df["MACD_hist"].abs().max()+1e-9)
    fig.add_trace(go.Bar(x=df.index,y=df["MACD_hist"]*ms+50,name="MACD柱",marker_color=hc,opacity=0.5),row=4,col=1)

    xs = df.index[0].replace(hour=9,minute=0,second=0) if not df.empty else None
    xe = df.index[0].replace(hour=13,minute=35,second=0) if not df.empty else None
    fig.update_layout(template="plotly_dark",height=760,
                      title=dict(text=f"📈 {ticker} 全盤ORB即時分析",font=dict(size=18,color="#fff")),
                      xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h",y=1.03,x=0,font=dict(size=11)),
                      margin=dict(l=8,r=110,t=60,b=8),
                      plot_bgcolor="#0e1117",paper_bgcolor="#0e1117",autosize=True)
    fig.update_xaxes(gridcolor="#1e2130",showgrid=True,range=[xs,xe] if xs else None,
                     rangebreaks=[dict(bounds=["sat","mon"]),dict(bounds=[13.5,9.0],pattern="hour")],
                     tickformat="%H:%M",dtick=1800000)
    fig.update_yaxes(gridcolor="#1e2130",showgrid=True)
    return fig


# ─── Session State ───────────────────────────────────────────────────────────
for k,v in [("signal_log",[]),("last_bar_ts",None),("level_cooldown",{}),
             ("sound_enabled",True),("email_cooldown",{}),
             ("price_alert_enabled",False),("price_alert_price",0.0),("price_alert_last_sent",None),
             ("price_alert_hi_enabled",False),("price_alert_hi_price",0.0),("price_alert_hi_last_sent",None)]:
    if k not in st.session_state: st.session_state[k]=v

LEVEL_SIGNAL_CATS={"RSI嚴重超買","RSI嚴重超賣","強制出場時間到","準備出場",
                   "過熱勿追","縮量開盤警示","VWAP跌破停損"}

# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 📊 全盤ORB即時分析")
    st.markdown("---")
    ticker   = st.text_input("股票代號",value="2330",max_chars=6).strip()
    interval = st.selectbox("K棒週期",["1m","5m","15m"],index=1)
    st.markdown("---")
    st.markdown("### 風險管理設定")
    capital  = st.number_input("交易資金 (元)",value=500000,step=50000,format="%d")
    risk_pct = st.slider("單筆風險 (%)",0.3,2.0,1.0,0.1)
    st.markdown("---")
    cs1,cs2=st.columns(2)
    with cs1:
        snd=st.toggle("🔊 聲音提示",value=st.session_state.sound_enabled)
        st.session_state.sound_enabled=snd
    with cs2:
        if st.button("🗑️ 清除記錄"):
            st.session_state.signal_log=[]
            st.session_state.level_cooldown={}
            st.session_state.email_cooldown={}
    st.markdown("---")
    st.markdown("### 🎯 趨勢確認過濾")
    trend_filter_on = st.toggle("啟用 ADX+VWAP斜率過濾進場",value=True,
                                key="trend_filter_toggle")
    adx_min = st.slider("ADX 盤整門檻（低於=擋進場）",10.0,35.0,20.0,1.0,
                        key="adx_min_slider")
    st.caption("僅過濾『進場型』突破訊號；停損/出場一律放行")
    st.markdown("---")
    st.markdown("### 🔔 限價提醒")
    pa_enabled = st.toggle("啟用限價 Email 提醒",
                           value=st.session_state.price_alert_enabled,
                           key="pa_toggle")
    if pa_enabled != st.session_state.price_alert_enabled:
        st.session_state.price_alert_enabled   = pa_enabled
        st.session_state.price_alert_last_sent = None  # 切換時重置
    pa_price = st.number_input("觸發價格（現價 ≤ 此值時通知）",
                                value=st.session_state.price_alert_price or 0.0,
                                min_value=0.0, step=0.1, format="%.1f",
                                key="pa_price_input")
    if pa_price != st.session_state.price_alert_price:
        st.session_state.price_alert_price     = pa_price
        st.session_state.price_alert_last_sent = None  # 改價時重置
    if st.session_state.price_alert_enabled and pa_price > 0:
        last_sent = st.session_state.price_alert_last_sent
        if last_sent is not None:
            import time as _time
            elapsed = _time.time() - last_sent
            st.success(f"✅ 已觸發，每60秒重送（{int(60-elapsed)}秒後下次）")
        else:
            st.info(f"📭 等待觸發：現價 ≤ {pa_price:.1f}")
        if st.button("🔄 停止提醒", key="pa_lo_stop"):
            st.session_state.price_alert_last_sent = None
            st.session_state.price_alert_enabled   = False

    st.markdown("**📈 股價高於等於通知**")
    pa_hi_enabled = st.toggle("啟用上限 Email 提醒",
                               value=st.session_state.price_alert_hi_enabled,
                               key="pa_hi_toggle")
    if pa_hi_enabled != st.session_state.price_alert_hi_enabled:
        st.session_state.price_alert_hi_enabled   = pa_hi_enabled
        st.session_state.price_alert_hi_last_sent = None
    pa_hi_price = st.number_input("觸發價格（現價 ≥ 此值時通知）",
                                   value=st.session_state.price_alert_hi_price or 0.0,
                                   min_value=0.0, step=0.1, format="%.1f",
                                   key="pa_hi_price_input")
    if pa_hi_price != st.session_state.price_alert_hi_price:
        st.session_state.price_alert_hi_price     = pa_hi_price
        st.session_state.price_alert_hi_last_sent = None
    if st.session_state.price_alert_hi_enabled and pa_hi_price > 0:
        last_hi    = st.session_state.price_alert_hi_last_sent
        if last_hi is not None:
            elapsed_hi = _time.time() - last_hi
            st.success(f"✅ 已觸發，每60秒重送（{int(60-elapsed_hi)}秒後下次）")
        else:
            st.info(f"📭 等待觸發：現價 ≥ {pa_hi_price:.1f}")
        if st.button("🔄 停止提醒", key="pa_hi_stop"):
            st.session_state.price_alert_hi_last_sent = None
            st.session_state.price_alert_hi_enabled   = False

    st.markdown("---")
    st.markdown("### 📊 周轉率設定")
    auto_shares=get_shares_outstanding(ticker) if ticker else 0
    auto_wan=int(auto_shares/10000) if auto_shares>0 else 0
    st.caption(f"自動取得：{auto_wan:,} 萬股" if auto_shares>0 else "⚠️ 無法自動取得")
    manual_wan=st.number_input("發行股數（萬股）",value=auto_wan if auto_wan>0 else 100000,
                                step=1000,min_value=1,format="%d")
    shares_outstanding=int(manual_wan)*10000
    if "email_enabled" not in st.session_state: st.session_state.email_enabled=True
    etog=st.toggle("📧 Gmail 通知（CRITICAL+HIGH）",value=st.session_state.email_enabled)
    st.session_state.email_enabled=etog
    if GMAIL_TO:
        st.caption(f"收件：{GMAIL_TO}")
    else:
        st.caption("⚠️ 未設定 GMAIL_USER/PASSWORD（請用 st.secrets 或環境變數）")
    st.markdown("---")
    now_tw=datetime.now(TW_TZ)
    mkt_open=now_tw.replace(hour=9,minute=0,second=0,microsecond=0)
    mkt_cls =now_tw.replace(hour=13,minute=30,second=0,microsecond=0)
    if mkt_open<=now_tw<=mkt_cls: st.success(f"🟢 台股交易中\n{now_tw.strftime('%H:%M:%S')}")
    else: st.info(f"🔵 非交易時段\n{now_tw.strftime('%H:%M:%S')}")
    st.markdown("### ⏰ 全盤關鍵節點")
    for t,note,ico in [
        ("09:00-09:30","ORB區間形成，觀察勿進","🟡"),
        ("09:30-12:30","早盤ORB突破/回測/噴出回測操作段","🟢"),
        ("11:30-12:30","同時建立NCR區間，ORB訊號持續有效","🟢"),
        ("12:30-13:20","NCR突破/午盤操作段","🟢"),
        ("13:20前",     "出清所有部位！","🔴"),
    ]:
        st.markdown(f"{ico} **{t}**  \n&nbsp;&nbsp;&nbsp;{note}")

# ─── Auto Refresh ────────────────────────────────────────────────────────────
refresh_count=st_autorefresh(interval=5000,limit=None,key="auto_refresh_5s")

# ─── Load Data ───────────────────────────────────────────────────────────────
# 先取前日量供 Vol_ratio 開盤期校正
prev_day_vol = get_prev_day_volume(ticker)

with st.spinner(""):
    df=fetch_and_calc(ticker, interval, prev_day_vol)
if df.empty:
    st.error(f"### ❌ 查詢失敗：{ticker}\n\n{df.attrs.get('error','無法取得資料')}")
    st.stop()

data_source=df.attrs.get("source","yfinance")
turnover=calc_turnover(df,shares_outstanding,source=data_source)
tr_cur=turnover["current_pct_adj"]; tr_cur_raw=turnover["current_pct"]
tr_est=turnover["estimated_pct"]
tr_label,tr_color=turnover_label(tr_est if tr_est>0 else tr_cur)

# ─── 計算 ORB / NCR (P1 修正：傳入 interval) ─────────────────────────────────
rng          = calc_ranges(df, interval)
open5_vol        = rng.get("open5_vol",0)
prev_day_vol_adj = df.attrs.get("prev_day_vol_adj", prev_day_vol)
vol_unit         = df.attrs.get("vol_unit", 1)
open_vol_pct     = (open5_vol / prev_day_vol_adj * 100) if prev_day_vol_adj > 0 else 0
open_vol_ok      = open_vol_pct >= 10

# ─── 外盤 / 內盤 ─────────────────────────────────────────────────────────────
bav = get_fugle_bid_ask_vol(ticker)

# ─── 限價提醒（每30秒重送）────────────────────────────────────────────────────
_current_price = float(df.iloc[-1]["Close"])

# 下限提醒：現價 ≤ 設定值
if (st.session_state.price_alert_enabled
        and st.session_state.price_alert_price > 0
        and _current_price <= st.session_state.price_alert_price):
    _last = st.session_state.price_alert_last_sent
    if _last is None or (_time.time() - _last) >= 60:
        send_price_alert_email(ticker, _current_price, st.session_state.price_alert_price, "low")
        st.session_state.price_alert_last_sent = _time.time()

# 上限提醒：現價 ≥ 設定值
if (st.session_state.price_alert_hi_enabled
        and st.session_state.price_alert_hi_price > 0
        and _current_price >= st.session_state.price_alert_hi_price):
    _last_hi = st.session_state.price_alert_hi_last_sent
    if _last_hi is None or (_time.time() - _last_hi) >= 60:
        send_price_alert_email(ticker, _current_price, st.session_state.price_alert_hi_price, "high")
        st.session_state.price_alert_hi_last_sent = _time.time()

# ─── Signal Processing ───────────────────────────────────────────────────────
current_bar_ts=str(df.index[-1])
is_new_bar=(st.session_state.last_bar_ts!=current_bar_ts)
raw_signals=detect_signals(df,is_new_bar,rng,prev_day_vol_adj)
# v18: 趨勢確認過濾（單一注入點）
raw_signals, _blocked_signals = apply_trend_confirmation(
    raw_signals, df, st.session_state.get("trend_filter_toggle", True),
    st.session_state.get("adx_min_slider", 20.0))
now_tw=datetime.now(TW_TZ)

# 一次性訊號的 cooldown（避免持續類訊號連發）
new_signals=[]
for sig in raw_signals:
    if sig["category"] in LEVEL_SIGNAL_CATS:
        lt=st.session_state.level_cooldown.get(sig["category"])
        if lt is None or (now_tw-lt).total_seconds()>=60:  # v18: .seconds 會丟天數/跨午夜異常
            new_signals.append(sig)
            st.session_state.level_cooldown[sig["category"]]=now_tw
    else:
        new_signals.append(sig)

for sig in new_signals:
    already=any(e.get("category")==sig["category"] and e.get("bar")==current_bar_ts
                for e in st.session_state.signal_log)
    if already: continue
    entry={**sig,"time":now_tw.strftime("%H:%M:%S"),"bar":current_bar_ts,"price":float(df.iloc[-1]["Close"])}
    st.session_state.signal_log.insert(0,entry)
    # P0 修正：Email 冷卻 300 秒（避免同一類訊號短時間轟炸信箱）
    if st.session_state.get("email_enabled",True) and sig["priority"] in EMAIL_PRIORITY:
        cat=sig["category"]
        ls=st.session_state.email_cooldown.get(cat)
        if ls is None or (now_tw-ls).total_seconds()>=EMAIL_COOLDOWN:
            send_signal_email(sig,ticker,float(df.iloc[-1]["Close"]))
            st.session_state.email_cooldown[cat]=now_tw

st.session_state.signal_log=st.session_state.signal_log[:80]
if is_new_bar: st.session_state.last_bar_ts=current_bar_ts

# v16: 日型判斷（全頁共用）
_day_char, _day_diff = get_day_character(df)
composite=get_composite_signal(df,rng)
last=df.iloc[-1]; prev=df.iloc[-2] if len(df)>=2 else last
meta_symbol=df.attrs.get("symbol",f"{ticker}.TW")
meta_date  =df.attrs.get("used_date",str(now_tw.date()))
meta_today =df.attrs.get("is_today",True)
meta_source=df.attrs.get("source","yfinance")

# ─── Page Title ──────────────────────────────────────────────────────────────
stock_name=get_stock_name_tw(ticker)
name_part=f"　{stock_name}" if stock_name else ""
st.markdown(f"# 📈 即時儀表板 — {ticker}{name_part}"
            f"　<span style='font-size:16px;color:#8892a4'>({meta_symbol})</span>",
            unsafe_allow_html=True)

if not meta_today:
    st.warning(f"⚠️ **非交易日/今日尚無資料**　顯示最近交易日 **{meta_date}** 歷史資料。",icon="📅")

# ─── Status Bar ──────────────────────────────────────────────────────────────
dot_color="#00d48a" if meta_today else "#ffa500"
live_txt=("富果即時" if meta_source=="fugle" else "即時更新中") if meta_today else "歷史資料"
src_badge=('<span style="color:#4a9eff;font-size:11px">⚡富果API</span>'
           if meta_source=="fugle" else '<span style="color:#ffa500;font-size:11px">yfinance</span>')
st.markdown(f"""<div class="status-bar">
  <span><span class="live-dot" style="background:{dot_color}"></span>
    {live_txt}（每5秒）　最後刷新：{now_tw.strftime('%H:%M:%S')}　共刷新 {refresh_count} 次　{src_badge}
  </span>
  <span>K棒週期：{interval}　最新K棒：{df.index[-1].strftime('%H:%M')}　資料筆數：{len(df)}</span>
</div>""", unsafe_allow_html=True)

# ─── Alert Banners ───────────────────────────────────────────────────────────
if new_signals:
    if st.session_state.sound_enabled:
        hc=any(s["priority"]=="CRITICAL" for s in new_signals)
        hb=any(s["type"]=="BUY" for s in new_signals)
        freq=1200 if hc else (880 if hb else 440)
        st.markdown(f"<script>setTimeout(()=>playBeep({freq},.4),100);</script>",unsafe_allow_html=True)
    st.toast(f"{new_signals[0]['icon']} {new_signals[0]['category']}：{new_signals[0]['msg'][:40]}...")

_seen=set(); display_signals=[]
for e in st.session_state.signal_log:
    if e.get("bar")==current_bar_ts and e["category"] not in _seen:
        display_signals.append(e); _seen.add(e["category"])

if display_signals:
    st.markdown("### 🔔 即時訊號")
    for sig in display_signals:
        css=("alert-critical" if sig["priority"]=="CRITICAL" else
             "alert-buy" if sig["type"]=="BUY" else
             "alert-sell" if sig["type"]=="SELL" else "alert-warn")
        tlbl={"BUY":"▲ 進場訊號","SELL":"▼ 出場訊號","WARN":"⚠ 注意"}.get(sig["type"],"")
        tclr={"BUY":"#00d48a","SELL":"#ff6b35","WARN":"#ffc300"}.get(sig["type"],"#fff")
        st.markdown(f"""<div class="{css}">
          <div class="alert-title">{sig['icon']} {sig['category']}
            &nbsp;&nbsp;<span style="font-size:13px;color:{tclr};font-weight:600">{tlbl}</span></div>
          <div class="alert-msg">{sig['msg']}</div>
          <div class="alert-time">觸發時間：{sig['time']}</div>
        </div>""", unsafe_allow_html=True)
    st.markdown("---")

# ─── Metric Cards ────────────────────────────────────────────────────────────
chg=(last["Close"]-prev["Close"])/prev["Close"]*100
chg_color="#00d48a" if chg>=0 else "#ff4b4b"
score=composite["score"]; c_dir=composite["color"]

orb_h=rng.get("orb_high"); orb_l=rng.get("orb_low")
orb_h_str=f"{orb_h:.1f}" if orb_h else "形成中"
ncr_h=rng.get("ncr_high"); ncr_l=rng.get("ncr_low")
ncr_range_str=(f"{ncr_l:.1f}～{ncr_h:.1f}" if ncr_h and ncr_l else
               "整理中" if "11:30"<=now_tw.strftime("%H:%M")<"12:30" else "尚未形成")
ncr_color=("#ffa500" if ncr_h else "#8892a4")

orb_pos_color=("#00d48a" if orb_h and last["Close"]>orb_h else
               "#ff4b4b" if orb_h and last["Close"]<orb_h else "#ffa500")

ovol_str=f"{open_vol_pct:.1f}%" if prev_day_vol>0 and open5_vol>0 else "讀取中"
ovol_clr="#00d48a" if open_vol_ok else ("#ff4b4b" if prev_day_vol>0 and open5_vol>0 else "#8892a4")
ovol_delta="✅ ≥10% 量足" if open_vol_ok else ("❌ <10% 量弱" if prev_day_vol>0 and open5_vol>0 else "計算中")

cols=st.columns(8)
metrics=[
    ("現價",      f"{last['Close']:.1f}",      f"{chg:+.2f}%",                      chg_color),
    ("開盤量比",  ovol_str,                     ovol_delta,                           ovol_clr),
    ("VWAP",      f"{last['VWAP']:.1f}",
     "▲ 多方" if last["Close"]>last["VWAP"] else "▼ 空方",
     "#00d48a" if last["Close"]>last["VWAP"] else "#ff4b4b"),
    ("RSI(6)",    f"{last['RSI6']:.1f}",
     "超熱🔥" if last["RSI6"]>85 else ("超買" if last["RSI6"]>70 else ("超賣" if last["RSI6"]<30 else "正常")),
     "#ff2222" if last["RSI6"]>85 else ("#ff4b4b" if last["RSI6"]>70 else ("#00d48a" if last["RSI6"]<30 else "#8892a4"))),
    ("ORB高點",   orb_h_str,
     "▲ 站上ORB" if orb_h and last["Close"]>orb_h else ("▼ 跌破ORB" if orb_h else "等待"),
     orb_pos_color),
    ("NCR整理區", ncr_range_str,               "午間11:30-12:30",                    ncr_color),
    ("量比",      f"{last['Vol_ratio']:.2f}x",
     "爆量" if last["Vol_ratio"]>=1.5 else ("正常" if last["Vol_ratio"]>=0.7 else "縮量"),
     "#00d48a" if last["Vol_ratio"]>=1.5 else ("#ffa500" if last["Vol_ratio"]>=0.7 else "#ff4b4b")),
    ("即時周轉率",f"{tr_cur:.2f}%" if tr_cur>0 else "讀取中",
     (f"預估{tr_est:.1f}%" if tr_est>0 else "—")+f" {tr_label}", tr_color),
]
for col,(label,val,delta,color) in zip(cols,metrics):
    col.markdown(f"""<div class="metric-card">
      <div class="metric-label">{label}</div>
      <div class="metric-value">{val}</div>
      <div class="metric-delta" style="color:{color}">{delta}</div>
    </div>""", unsafe_allow_html=True)

# ─── 外盤 / 內盤橫幅 ─────────────────────────────────────────────────────────
if bav["ok"]:
    _ask_pct = bav["ask_pct"]; _bid_pct = bav["bid_pct"]
    _ask_vol = bav["ask_vol"]; _bid_vol = bav["bid_vol"]
    if _ask_pct >= 55:
        _bav_color = "#00d48a"; _bav_label = "多方主動 🔺"
    elif _ask_pct <= 45:
        _bav_color = "#ff4b4b"; _bav_label = "空方主動 🔻"
    else:
        _bav_color = "#ffa500"; _bav_label = "多空拉鋸 ↔"
    _ask_bar = int(_ask_pct); _bid_bar = 100 - _ask_bar
    st.markdown(f"""
    <div style="background:#1e2130;border:1px solid {_bav_color};border-radius:10px;
                padding:10px 20px;margin:6px 0">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span style="color:#8892a4;font-size:12px">外盤／內盤（主動買賣力道）</span>
        <span style="color:{_bav_color};font-size:13px;font-weight:700">{_bav_label}</span>
        <span style="color:#8892a4;font-size:12px">外盤 {_ask_vol:,} 張 ／ 內盤 {_bid_vol:,} 張</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span style="color:#00d48a;font-size:15px;font-weight:700;min-width:58px">外 {_ask_pct:.1f}%</span>
        <div style="flex:1;display:flex;height:12px;border-radius:6px;overflow:hidden">
          <div style="width:{_ask_bar}%;background:#00d48a"></div>
          <div style="width:{_bid_bar}%;background:#ff4b4b"></div>
        </div>
        <span style="color:#ff4b4b;font-size:15px;font-weight:700;min-width:58px;text-align:right">內 {_bid_pct:.1f}%</span>
      </div>
    </div>""", unsafe_allow_html=True)

# ─── v16 日型判斷橫幅 ────────────────────────────────────────────────────────
_dc_color = {"bullish":"#00d48a", "bearish":"#ff4b4b", "neutral":"#ffa500"}[_day_char]
_dc_icon  = {"bullish":"🟢 多方主導日", "bearish":"🔴 空方主導日", "neutral":"🟡 中立觀望日"}[_day_char]
_dc_hint  = {
    "bullish": "早盤均價高於ORB中線+0.5%，多方偏強，優先做多訊號",
    "bearish": "早盤均價低於ORB中線-0.5%，空方偏強，多方訊號暫緩",
    "neutral": "早盤均價接近ORB中線，多空均衡，訊號需配合其他確認"
}[_day_char]
_now_hm_dc = now_tw.strftime("%H:%M")
_enough_data = _now_hm_dc >= "10:00"
if _enough_data:
    st.markdown(f"""<div style="background:linear-gradient(135deg,#1e2130,#0e1117);
        border:2px solid {_dc_color};border-radius:10px;
        padding:10px 20px;margin:6px 0;display:flex;align-items:center;gap:16px;">
      <span style="font-size:18px;font-weight:800;color:{_dc_color}">{_dc_icon}</span>
      <span style="color:#ccc;font-size:13px">{_dc_hint}</span>
      <span style="color:#666;font-size:12px;margin-left:auto">早盤偏離:{_day_diff:+.2f}%</span>
    </div>""", unsafe_allow_html=True)
else:
    st.markdown(f"""<div style="background:#1e2130;border:1px solid #3a3a5a;border-radius:10px;
        padding:8px 20px;margin:6px 0;">
      <span style="color:#ffa500;font-size:14px">⏳ 日型建立中（10:00後更新）— 目前 {_now_hm_dc}</span>
    </div>""", unsafe_allow_html=True)

# ─── v18 趨勢確認狀態橫幅 ────────────────────────────────────────────────────
_adx_now   = last.get("ADX", float("nan"))
_slope_now = last.get("VWAP_slope", float("nan"))
_atr_now   = last.get("ATR", float("nan"))
_atrp_now  = last.get("ATR_pct", float("nan"))
_adx_txt   = f"{_adx_now:.0f}" if not pd.isna(_adx_now) else "—"
_slope_txt = f"{_slope_now:+.2f}%" if not pd.isna(_slope_now) else "—"
_atr_txt   = f"{_atr_now:.2f}（{_atrp_now:.2f}%）" if not pd.isna(_atr_now) else "—"
_adx_min_now = st.session_state.get("adx_min_slider", 20.0)
if not pd.isna(_adx_now):
    if _adx_now >= _adx_min_now + 5:
        _tr_state, _tr_clr = "趨勢明確 ✅ 突破可信", "#00d48a"
    elif _adx_now >= _adx_min_now:
        _tr_state, _tr_clr = "趨勢偏弱 ⚠ 需配合量能", "#ffa500"
    else:
        _tr_state, _tr_clr = "盤整中 🚫 突破假訊號率高", "#ff4b4b"
else:
    _tr_state, _tr_clr = "資料不足", "#8892a4"
st.markdown(f"""<div style="background:#1e2130;border:1px solid {_tr_clr};border-radius:10px;
    padding:9px 20px;margin:6px 0;display:flex;align-items:center;gap:20px;flex-wrap:wrap;">
  <span style="color:{_tr_clr};font-size:14px;font-weight:700">📊 趨勢確認：{_tr_state}</span>
  <span style="color:#aaa;font-size:13px">ADX <b style="color:#fff">{_adx_txt}</b>（門檻{_adx_min_now:.0f}）</span>
  <span style="color:#aaa;font-size:13px">VWAP斜率 <b style="color:#fff">{_slope_txt}</b></span>
  <span style="color:#aaa;font-size:13px">ATR <b style="color:#fff">{_atr_txt}</b></span>
</div>""", unsafe_allow_html=True)
if _blocked_signals:
    _bl = "　|　".join(_blocked_signals[:4])
    st.markdown(f"""<div style="background:#2a1808;border:1px dashed #ffa500;border-radius:8px;
        padding:7px 16px;margin:4px 0;color:#ffa500;font-size:12px">
      🛡️ 已過濾未確認進場訊號：{_bl}</div>""", unsafe_allow_html=True)

st.markdown("---")
st.plotly_chart(make_chart(df,ticker,st.session_state.signal_log,rng), use_container_width=True)

# ─── Tabs ────────────────────────────────────────────────────────────────────
tab1,tab2,tab3,tab4,tab5=st.tabs(
    ["📋 全盤進場條件","💰 風險計算器","📜 訊號記錄","📊 指標明細","🔄 周轉率分析"])

with tab1:
    entry_ready =composite.get("entry_ready",False)
    entry_dir   =composite.get("entry_dir","")
    entry_type  =composite.get("entry_type","")
    long_ready  =composite.get("long_ready",False)
    short_ready =composite.get("short_ready",False)
    now_hm=now_tw.strftime("%H:%M")

    if entry_ready and "09:15"<=now_hm<="13:20":
        lamp_color="#00d48a" if entry_dir=="做多" else "#ff4b4b"
        lamp_icon ="🟢" if entry_dir=="做多" else "🔴"
        anim="pulse-green" if entry_dir=="做多" else "pulse-red"
        bg_grad="135deg,#082d1a,#0f4a2a" if entry_dir=="做多" else "135deg,#2d0808,#4a0f0f"
        st.markdown(f"""
        <div style="background:linear-gradient({bg_grad});
            border:2px solid {lamp_color};border-radius:14px;padding:20px 28px;margin-bottom:12px;
            animation:{anim} 1s infinite;">
          <div style="font-size:26px;font-weight:800;color:{lamp_color}">{lamp_icon} {entry_type} — {entry_dir}！</div>
          <div style="color:#ddd;margin-top:8px;font-size:14px">進場條件達成，進場價 ≈ {last['Close']:.1f}</div>
        </div>""", unsafe_allow_html=True)
    else:
        phase=("ORB區間形成中（09:00-09:30）" if now_hm<"09:30" else
               "早盤段（ORB突破/回測）" if now_hm<"11:30" else
               "午間整理段（ORB訊號持續+NCR建立中）" if now_hm<"12:30" else "午盤段（NCR突破）")
        st.markdown(f"""
        <div style="background:#1e2130;border:1px solid #3a3a5a;border-radius:14px;
            padding:16px 24px;margin-bottom:12px;">
          <div style="font-size:18px;font-weight:700;color:#ffa500">
            ⏳ 等待訊號… 目前階段：{phase}（多空分 {score}/5）
          </div>
          <div style="color:#8892a4;margin-top:6px;font-size:13px">
            早盤+午間整理(09:30-12:30)：ORB突破／回測支撐／噴出回測VWAP／ORB跌破／ORB回測壓力<br>
            午盤(12:30後)：NCR整理突破／NCR整理跌破／VWAP+RSI全日買點
          </div>
        </div>""", unsafe_allow_html=True)

    col_a, col_b = st.columns([1, 2])

    with col_a:
        orb_h=rng.get("orb_high"); orb_l=rng.get("orb_low")
        orb_h_s=f"{orb_h:.1f}" if orb_h else "—"
        orb_l_s=f"{orb_l:.1f}" if orb_l else "—"
        op=rng.get("opening_price"); op_s=f"{op:.1f}" if op else "—"
        orb_rng=f"{orb_h-orb_l:.1f}" if orb_h and orb_l else "—"
        above_orb=orb_h and last["Close"]>orb_h
        orb_pos_txt="▲ 站上ORB多方有利" if above_orb else ("▼ 跌破ORB空方主導" if orb_h else "等待")
        orb_pos_clr="#00d48a" if above_orb else ("#ff4b4b" if orb_h else "#ffa500")
        orb_est_txt="✅ 已建立" if rng.get("orb_established") else "⏳ 形成中"
        st.markdown(f"""<div class="range-box" style="border:1px solid #00ff88">
          <div style="color:#00ff88;font-weight:700;font-size:13px;margin-bottom:8px">
            📐 ORB 開盤區間 {orb_est_txt}（截止 {rng.get('orb_end','09:30')}）
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">▲ 高點</span><span style="color:#fff;font-weight:700">{orb_h_s}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">▼ 低點</span><span style="color:#fff;font-weight:700">{orb_l_s}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">◆ 開盤</span><span style="color:#f0e040;font-weight:700">{op_s}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">↕ 幅度</span><span style="color:#ffa500">{orb_rng}</span>
          </div>
          <div style="margin-top:8px;font-size:12px;font-weight:700;color:{orb_pos_clr}">{orb_pos_txt}</div>
        </div>""", unsafe_allow_html=True)

        ncr_h=rng.get("ncr_high"); ncr_l=rng.get("ncr_low")
        ncr_h_s=f"{ncr_h:.1f}" if ncr_h else "—"
        ncr_l_s=f"{ncr_l:.1f}" if ncr_l else "—"
        ncr_rng=f"{ncr_h-ncr_l:.1f}" if ncr_h and ncr_l else "—"
        ncr_est_txt=("✅ 已建立" if rng.get("ncr_established") else
                     "⏳ 形成中(11:30-12:30)" if "11:30"<=now_hm<"12:30" else "尚未形成")
        ncr_pos=""
        if ncr_h and ncr_l:
            if last["Close"]>ncr_h: ncr_pos="▲ 突破NCR高，做多有效"
            elif last["Close"]<ncr_l: ncr_pos="▼ 跌破NCR低，做空有效"
            else: ncr_pos="◆ 整理區間內"
        ncr_pos_clr=("#00d48a" if last["Close"]>(ncr_h or 0) else
                     "#ff4b4b" if ncr_l and last["Close"]<ncr_l else "#ffa500")
        st.markdown(f"""<div class="range-box" style="border:1px solid #ffa500;margin-top:8px">
          <div style="color:#ffa500;font-weight:700;font-size:13px;margin-bottom:8px">
            🌇 NCR 午間整理區間 {ncr_est_txt}
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">▲ 高點</span><span style="color:#fff;font-weight:700">{ncr_h_s}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">▼ 低點</span><span style="color:#fff;font-weight:700">{ncr_l_s}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:13px;margin:3px 0">
            <span style="color:#aaa">↕ 幅度</span><span style="color:#ffa500">{ncr_rng}</span>
          </div>
          <div style="margin-top:8px;font-size:12px;font-weight:700;color:{ncr_pos_clr}">{ncr_pos if ncr_pos else "12:30後觸發NCR突破訊號"}</div>
        </div>""", unsafe_allow_html=True)

        if prev_day_vol>0:
            vf=min(int(open_vol_pct/20*100),100)
            vc="#00d48a" if open_vol_ok else "#ff4b4b"
            st.markdown(f"""<div class="metric-card" style="margin-top:8px;border-left-color:{vc}">
              <div class="metric-label">開盤5分量佔前日（需≥10%）</div>
              <div class="metric-value" style="color:{vc}">{open_vol_pct:.1f}%</div>
              <div class="score-bar-bg"><div class="score-bar-fill" style="width:{vf}%;background:{vc}"></div></div>
              <div class="metric-delta" style="color:#8892a4">{open5_vol:,}張 / 前日{prev_day_vol_adj:,}張</div>
            </div>""", unsafe_allow_html=True)

    with col_b:
        now_hm2=now_tw.strftime("%H:%M")
        vwap_dev_now=(last["Close"]-last["VWAP"])/last["VWAP"]*100
        above_vwap_now=last["Close"]>=last["VWAP"]
        op2=rng.get("opening_price"); op2_s=f"{op2:.1f}" if op2 else "—"
        above_open_now=op2 and last["Close"]>=op2*0.997
        orb_h2=rng.get("orb_high"); orb_h2_s=f"{orb_h2:.1f}" if orb_h2 else "—"
        ncr_h2=rng.get("ncr_high"); ncr_h2_s=f"{ncr_h2:.1f}" if ncr_h2 else "—"
        ncr_l2=rng.get("ncr_low"); ncr_l2_s=f"{ncr_l2:.1f}" if ncr_l2 else "—"

        st.markdown("#### 📌 早盤+午間整理進場條件（09:30-12:30）")
        morning_checks=[
            ("ORB已建立",                   rng.get("orb_established",False),
             f"{rng.get('orb_end','09:30')}後至少數根K棒",           "必要前提"),
            ("開盤量≥10%（主力入場）",       open_vol_ok,
             f"開盤量{open_vol_pct:.1f}%",  "量不足突破不可信"),
            ("A. 突破ORB高（截止11:00）", orb_h2 is not None and last["Close"]>(orb_h2 or 0),
             f"現價{last['Close']:.1f} vs ORB高{orb_h2_s}", "RSI>55+VWAP乖離≤3%+量≥1.5x"),
            ("B. ORB回測後再站上",           orb_h2 is not None and last["Close"]>(orb_h2 or 0)
             and prev["Close"]<=(orb_h2 or 0),
             f"前棒跌回ORB高{orb_h2_s}以下後彈", "陽線確認+RSI>45+量≥1.0x"),
            ("C. VWAP上方(回測)",            above_vwap_now,
             f"現價{last['Close']:.1f} vs VWAP{last['VWAP']:.1f}", "RSI 45-68+陽線吞噬+拉回≥0.5%"),
            ("C. 開盤價上方",               bool(above_open_now),
             f"開盤價{op2_s}",              "回測不破開盤價是強勢"),
        ]
        for label,passed,val_txt,hint in morning_checks:
            icon="✅" if passed else "❌"
            color="#00d48a" if passed else "#ff4b4b"
            st.markdown(f'<div style="margin:4px 0">'
                        f'<span style="color:{color};font-size:14px">{icon} <b>{label}</b></span>'
                        f'<span style="color:#8892a4;font-size:12px;margin-left:10px">{val_txt}</span>'
                        f'<br><span style="color:#555;font-size:11px;padding-left:22px">{hint}</span>'
                        f'</div>', unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("#### 📌 空方進場條件（v15 新增）")
        chk_now = _check_entry_conditions(df, rng)
        orb_l2 = rng.get("orb_low"); orb_l2_s = f"{orb_l2:.1f}" if orb_l2 else "—"
        ncr_l3 = rng.get("ncr_low"); ncr_l3_s = f"{ncr_l3:.1f}" if ncr_l3 else "—"
        short_checks=[
            ("A. 跌破ORB低（截止11:00）", chk_now.get("orb_breakdown",False),
             f"現價{last['Close']:.1f} vs ORB低{orb_l2_s}", "RSI<40+VWAP乖離<-0.5%+量≥1.5x"),
            ("B. ORB回測壓力後再跌（需先前跌破）", chk_now.get("orb_retest_dn",False),
             f"前棒反彈至ORB低{orb_l2_s}附近後跌破", "RSI<45+陰線確認+量≥1.0x"),
            ("D. 跌破NCR低點沖賣",       chk_now.get("ncr_breakdown",False),
             f"現價{last['Close']:.1f} vs NCR低{ncr_l3_s}", "RSI<45+VWAP下方+量≥1.2x"),
        ]
        for label,passed,val_txt,hint in short_checks:
            icon="✅" if passed else "❌"
            color="#ff6b35" if passed else "#ff4b4b"
            st.markdown(f'<div style="margin:4px 0">'
                        f'<span style="color:{color};font-size:14px">{icon} <b>{label}</b></span>'
                        f'<span style="color:#8892a4;font-size:12px;margin-left:10px">{val_txt}</span>'
                        f'<br><span style="color:#555;font-size:11px;padding-left:22px">{hint}</span>'
                        f'</div>', unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("#### 📌 午盤進場條件（12:30-13:20）")
        ncr_est2=rng.get("ncr_established",False)
        afternoon_checks=[
            ("NCR已建立（12:30後）",         ncr_est2,
             "11:30-12:30整理≥2根K棒",       "必要前提"),
            ("D. 突破NCR高點",              ncr_h2 is not None and last["Close"]>(ncr_h2 or 0),
             f"現價{last['Close']:.1f} vs NCR高{ncr_h2_s}", "RSI>55+量≥1.2x"),
            ("D. 跌破NCR低點(沖賣)",        ncr_l2 is not None and last["Close"]<(ncr_l2 or 0),
             f"現價{last['Close']:.1f} vs NCR低{ncr_l2_s}", "RSI<45+量≥1.2x"),
            ("E. VWAP+RSI全日買點",         last["RSI6"]<30 and abs(vwap_dev_now)<=0.3,
             f"RSI6={last['RSI6']:.1f}，VWAP差{abs(vwap_dev_now):.2f}%", "RSI<30+靠近VWAP+量≥1.0x"),
        ]
        for label,passed,val_txt,hint in afternoon_checks:
            icon="✅" if passed else "❌"
            color="#00d48a" if passed else "#ff4b4b"
            st.markdown(f'<div style="margin:4px 0">'
                        f'<span style="color:{color};font-size:14px">{icon} <b>{label}</b></span>'
                        f'<span style="color:#8892a4;font-size:12px;margin-left:10px">{val_txt}</span>'
                        f'<br><span style="color:#555;font-size:11px;padding-left:22px">{hint}</span>'
                        f'</div>', unsafe_allow_html=True)

        st.markdown("---")
        st.markdown(f"""
| 停利/停損 | 條件 | 現值 |
|-----------|------|------|
| 💰 先出一半 | BB上軌外+RSI>85 | RSI={last['RSI6']:.0f}，BB上軌={last['BB_upper']:.1f} |
| 🚨 全數出清 | 量縮+近高點+RSI降 | 量比={last['Vol_ratio']:.2f}x |
| ⚠️ 開始減碼 | 跌破MA5 | MA5={last['MA5']:.1f} |
| 🔴 立即停損 | 明顯跌破VWAP(>0.2%)+帶量 | VWAP={last['VWAP']:.1f} |
| 🔴 假突破砍倉 | 真實突破後明顯跌回ORB | ORB高={orb_h2_s} |
| 🟢 假跌破回補 | 真實跌破後明顯漲回ORB | ORB低={orb_l_s if rng.get("orb_low") else "—"} |
""")
        st.error("**三不原則：不追過熱（RSI>90+乖離>3%）／不攤平（只停損換股）／不留倉（13:20前出清）**")
        st.warning(
            "📐 **統計誠實聲明**：本策略多數參數（09:30-09:35 窗口、RSI 62 vs 68、"
            "10:30-11:00 等）源自『單一檔股票、約 59 天』的回測。樣本數小"
            "（部分子訊號 n<30，『午盤 100% 勝率』可能僅 3~5 筆），統計上**不具顯著性**，"
            "屬過度擬合風險區。實盤請：①把這些數字當『假設』非『規律』 "
            "②至少用多檔股票、跨不同行情（多/空/盤整）做樣本外驗證 "
            "③以 ATR 動態停損與每日虧損上限為主要保命線，不依賴單一參數。")

# ── Tab 2: 風險計算器 ────────────────────────────────────────────────────────
with tab2:
    c1,c2,c3=st.columns(3)
    with c1: entry_price=st.number_input("進場價",value=float(round(last["Close"],1)),step=0.5)
    with c2: dir_sel=st.selectbox("操作方向",["做多","做空"],
                                   index=0 if composite["direction"] in ("強烈做多","偏多觀望","中立觀望") else 1)
    with c3: cr=st.slider("風險比例 (%)",0.3,2.0,risk_pct,0.1,key="risk2")
    _atr_val = float(last.get("ATR", 0.0) or 0.0)
    s1,s2=st.columns([1,1])
    with s1:
        stop_mode=st.radio("停損模式",["ATR動態(推薦)","固定%"],horizontal=True,
                           index=0 if _atr_val>0 else 1,key="stop_mode")
    with s2:
        atr_mult=st.slider("ATR 倍數",1.0,3.0,1.5,0.1,key="atr_mult_slider")
    _mode = "atr" if stop_mode.startswith("ATR") and _atr_val>0 else "fixed"
    res=risk_calc(entry_price,dir_sel,capital,cr,mode=_mode,
                  atr=_atr_val,atr_mult=atr_mult)
    _sd_pct = res["stop_dist"]/entry_price*100 if entry_price else 0
    _mode_lbl = (f"ATR {_atr_val:.2f}×{atr_mult:.1f} ≈ {_sd_pct:.2f}%"
                 if _mode=="atr" else f"固定 -{cr:.1f}%")
    r1,r2,r3,r4=st.columns(4)
    r1.metric("停損價",f"{res['stop']:.2f}",_mode_lbl)
    r2.metric("目標1(2R)",f"{res['tp1']:.2f}",f"+{_sd_pct*2:.2f}%")
    r3.metric("目標2(3R)",f"{res['tp2']:.2f}",f"+{_sd_pct*3:.2f}%")
    r4.metric("建議張數",f"{int(res['shares']//1000)} 張",f"≈{res['cost']:,.0f}元")
    if _mode=="atr":
        st.caption(f"💡 停損距離隨波動度自適應：當前 ATR={_atr_val:.2f}（{last.get('ATR_pct',0):.2f}%）"
                   f"，部位已反推使單筆最大虧損固定在資金×{cr:.1f}%")
    st.markdown("---")
    e1,e2,e3=st.columns(3)
    bp=(entry_price*1.001425/(entry_price*0.997075)-1)*100
    e1.metric("最大虧損",f"{res['max_loss']:,.0f}元",f"資金×{cr:.1f}%")
    e2.metric("手續費+稅",f"{res['fee']:,.0f}元","0.285%+0.15%")
    e3.metric("損益兩平需漲",f"{bp:.2f}%","合計0.435%")
    st.markdown("---")
    st.warning(f"**每日虧損上限：{capital*0.02:,.0f}元**（資金×2%）　達到立即停手！")

# ── Tab 3: 訊號記錄 ──────────────────────────────────────────────────────────
with tab3:
    log=st.session_state.signal_log
    if not log:
        st.info("目前尚無訊號記錄，等待即時訊號觸發中...")
    else:
        bc=sum(1 for s in log if s["type"]=="BUY"); sc_=sum(1 for s in log if s["type"]=="SELL")
        st.markdown(f"**本日訊號統計：** 做多 {bc} 次　做空/出場 {sc_} 次　合計 {len(log)} 次")
        st.markdown("---")
        for entry in log[:40]:
            css=("log-entry-buy" if entry["type"]=="BUY" else
                 "log-entry-sell" if entry["type"]=="SELL" else "log-entry-warn")
            tt="▲ 進場" if entry["type"]=="BUY" else ("▼ 出場" if entry["type"]=="SELL" else "⚠ 注意")
            ps=f"　💰 {entry['price']:.1f}" if "price" in entry else ""
            st.markdown(f"""<div class="{css}">
              <span class="log-time">{entry['time']}</span>
              <strong>{entry['icon']} {entry['category']}</strong>
              <span style="color:#aaa;font-size:12px">{tt}</span>
              <span style="color:#f0e040;font-size:13px;font-weight:700">{ps}</span><br>
              <span style="color:#ccc;font-size:12px">{entry['msg']}</span>
            </div>""", unsafe_allow_html=True)

# ── Tab 4: 指標明細 ──────────────────────────────────────────────────────────
with tab4:
    dc=["Close","Volume","VWAP","MA5","MA10","BB_upper","BB_lower","RSI6","RSI14","K","D","MACD_hist","Vol_ratio","ATR","ADX","VWAP_slope"]
    disp=df[dc].tail(25).copy(); disp.index=disp.index.strftime("%H:%M")
    disp.columns=["收盤","成交量","VWAP","MA5","MA10","BB上軌","BB下軌","RSI6","RSI14","K","D","MACD柱","量比","ATR","ADX","VWAP斜率"]
    def hl(val):
        if isinstance(val,float):
            if val>85: return "color:#ff2222;font-weight:bold"
            if val>70: return "color:#ff4b4b"
            if val<30: return "color:#00d48a;font-weight:bold"
        return ""
    st.dataframe(disp.round(2).style.map(hl,subset=["RSI6","RSI14","K","D"]),
                 use_container_width=True,height=450)
    st.caption(f"來源：{meta_source}　更新：{df.index[-1].strftime('%Y-%m-%d %H:%M')}　週期：{interval}")

# ── Tab 5: 周轉率 ────────────────────────────────────────────────────────────
with tab5:
    st.markdown("### 🔄 即時周轉率分析")
    if shares_outstanding<=0:
        st.warning("請在左側欄輸入發行股數。")
    else:
        t1,t2,t3,t4=st.columns(4)
        _if=turnover["source"]=="fugle"; _cr=turnover["correction"]
        t1.markdown(f"""<div class="metric-card" style="border-left-color:{tr_color}">
          <div class="metric-label">{'截至目前周轉率' if _if else f'校正後(×{_cr})'}</div>
          <div class="metric-value" style="color:{tr_color}">{tr_cur:.3f}%</div>
          <div class="metric-delta" style="color:#8892a4">{'⚡富果精準' if _if else f'原始{tr_cur_raw:.3f}%×{_cr}'}</div>
        </div>""", unsafe_allow_html=True)
        t2.markdown(f"""<div class="metric-card" style="border-left-color:{tr_color}">
          <div class="metric-label">預估全日周轉率</div>
          <div class="metric-value" style="color:{tr_color}">{tr_est:.2f}%</div>
          <div class="metric-delta" style="color:{tr_color}">{tr_label}</div>
        </div>""", unsafe_allow_html=True)
        t3.markdown(f"""<div class="metric-card">
          <div class="metric-label">發行股數</div>
          <div class="metric-value">{shares_outstanding//10000:,}</div>
          <div class="metric-delta" style="color:#8892a4">萬股</div>
        </div>""", unsafe_allow_html=True)
        t4.markdown(f"""<div class="metric-card">
          <div class="metric-label">累計成交量</div>
          <div class="metric-value">{turnover['total_vol_adj']//10000:,}</div>
          <div class="metric-delta" style="color:#8892a4">萬股</div>
        </div>""", unsafe_allow_html=True)
        st.success("⚡ 富果精準值" if _if else f"⚙️ yfinance校正模式（×{_cr}）")
        st.markdown("---")
        pb=turnover["per_bar"].tail(40)
        if not pb.empty:
            bc2=["#8892a4" if v<0.01 else ("#ffa500" if v<0.05 else ("#4a9eff" if v<0.1 else ("#00d48a" if v<0.3 else "#ff4b4b"))) for v in pb]
            fig_tr=go.Figure(go.Bar(x=[t.strftime("%H:%M") for t in pb.index],y=pb.values*100,
                                    marker_color=bc2,name="每棒周轉率(%)"))
            cum=pb.cumsum()
            fig_tr.add_trace(go.Scatter(x=[t.strftime("%H:%M") for t in cum.index],y=cum.values*100,
                                         name="累計(%)",line=dict(color="#f0e040",width=2),yaxis="y2"))
            fig_tr.update_layout(template="plotly_dark",height=280,margin=dict(l=0,r=0,t=20,b=0),
                plot_bgcolor="#0e1117",paper_bgcolor="#0e1117",legend=dict(orientation="h",y=1.1),
                yaxis=dict(title="每棒(%)",gridcolor="#1e2130"),
                yaxis2=dict(title="累計(%)",overlaying="y",side="right",gridcolor="#1e2130"),
                xaxis=dict(gridcolor="#1e2130"))
            st.plotly_chart(fig_tr,use_container_width=True)

st.markdown("---")
st.markdown("<div style='text-align:center;color:#3a3a5a;font-size:12px'>"
            "⚠️ 本工具僅供技術分析參考，不構成投資建議。當沖風險極高，請謹慎操作並嚴守停損。"
            "</div>", unsafe_allow_html=True)
