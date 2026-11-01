import os
import requests
import yfinance as yf
import pandas as pd
import xml.etree.ElementTree as ET
from google import genai

# ── 1. API設定（GitHub Secretsから安全に取得） ──
LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
USER_ID = "U97e66f5d50a462734d78a10e3e66f77b"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ── 2. 保有銘柄リスト（手仕舞い・損切り監視） ──
MY_PORTFOLIO = {
    "NFLX": {"shares": 2, "entry": 86.91},
    "AMD":  {"shares": 1, "entry": 105.92},
    "NVDA": {"shares": 5, "entry": 117.38},
    "RBLX": {"shares": 10, "entry": 28.27},
    "JOBY": {"shares": 70, "entry": 6.55},
    "LUNR": {"shares": 10, "entry": 9.43}
}

# ── 3. 新規買い候補の監視リスト（米国株中心） ──
WATCH_UNIVERSE = {
    "US_MEGA": ["NVDA", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "AVGO", "TSLA"],
    "US_SAAS": ["PLTR", "CRWD", "PANW", "NET", "SNOW", "MDB", "DDOG", "SHOP"],
    "US_HARDWARE_INFRA": ["ARM", "MRVL", "VST"],
    "US_MOMENTUM": ["APP", "ASTS", "COIN", "UBER", "HOOD", "RDDT", "RKLB", "LLY"],
    "JP_REF": ["8035.T", "6857.T", "8306.T"]
}

ALL_WATCH_TICKERS = [t for group in WATCH_UNIVERSE.values() for t in group]


def send_line_message(text):
    """LINE Messaging API経由で通知をプッシュ送信"""
    if not LINE_ACCESS_TOKEN:
        print("LINE_ACCESS_TOKEN未設定")
        return
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {"to": USER_ID, "messages": [{"type": "text", "text": text}]}
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"LINE送信エラー: {e}")


def get_sec_recent_filings(ticker_symbol):
    """
    SEC EDGAR公式RSSフィードから直近の適時開示（8-K, 10-Q, 10-K等）を取得
    """
    if ticker_symbol.endswith(".T"):
        return ""
    try:
        url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker_symbol}&type=&dateb=&owner=exclude&start=0&count=5&output=atom"
        # SEC EDGARのAPI利用規約に準拠したUser-Agentヘッダー
        headers = {"User-Agent": "PersonalTradingBot trader@example.com"}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code != 200:
            return ""

        root = ET.fromstring(res.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        
        filings = []
        for entry in entries[:2]:  # 直近2件の重要開示
            title_node = entry.find("atom:title", ns)
            updated_node = entry.find("atom:updated", ns)
            if title_node is not None and title_node.text:
                title = title_node.text.strip()
                date_str = updated_node.text[:10] if updated_node is not None else ""
                filings.append(f"[{date_str}] {title}")
        
        return "\n".join(filings)
    except Exception:
        return ""


def get_stock_news(ticker_obj):
    """yfinance経由で直近の金融ニュース・見出しを抽出"""
    try:
        news_list = getattr(ticker_obj, "news", [])
        if not news_list:
            return ""
        headlines = []
        for item in news_list[:3]:  # 直近3件のニュース
            title = item.get("title", "")
            publisher = item.get("publisher", "")
            if title:
                headlines.append(f"- {title} ({publisher})")
        return "\n".join(headlines)
    except Exception:
        return ""


def analyze_with_gemini(ticker_symbol, fundamental_context="", news_context="", sec_context=""):
    """
    SEC開示・直近ニュース・財務データを前提条件としてAIに流し込み超短縮要約
    """
    if not GEMINI_API_KEY:
        return "AI未設定"

    prompt = f"""
あなたは米国株スイング専属アナリストです。銘柄「{ticker_symbol}」の最新状況を要約してください。

【前提データ（公式一次情報・直近報道）】
{fundamental_context}

[SEC公式開示(EDGAR)]
{sec_context if sec_context else "特記なし"}

[直近ニュース見出し]
{news_context if news_context else "特記なし"}

【出力条件】
・日本語50〜70文字厳守
・冒頭に【好調】【警戒】【中立】のいずれかを付与
・開示やニュースから「株価・出来高変動の主因（好決算、契約、格付け、悪材料等）」を1行で要約
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text.strip().replace("\n", " ")
    except Exception as e:
        return f"AIエラー: {str(e)[:30]}"


def get_usdjpy_rate():
    """ドル円為替レート自動取得"""
    try:
        usdjpy = yf.Ticker("USDJPY=X").history(period="5d")
        return round(usdjpy["Close"].iloc[-1], 1)
    except Exception:
        return 155.0


def run_trading_bot():
    usdjpy = get_usdjpy_rate()

    def to_jpy_str(usd_val):
        jpy = usd_val * usdjpy
        if abs(jpy) >= 10000:
            return f"約{jpy/10000:.1f}万"
        return f"約{round(jpy):,}円"

    round_val = lambda t, val: round(val, 0 if t.endswith(".T") else 1)

    # ── A. 地合いチェック（SPY） ──
    spy = yf.Ticker("SPY").history(period="3mo")
    spy["SMA20"] = spy["Close"].rolling(20).mean()
    market_is_bullish = spy["Close"].iloc[-1] > spy["SMA20"].iloc[-1]

    # ── B. 新規買い候補（SEC開示 ＋ 直近ニュース注入） ──
    buy_candidates = []
    for ticker in ALL_WATCH_TICKERS:
        is_jp = ticker.endswith(".T")
        stock = yf.Ticker(ticker)
        df = stock.history(period="6mo")
        if len(df) < 50:
            continue

        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA50"] = df["Close"].rolling(50).mean()
        df["Vol20"] = df["Volume"].rolling(20).mean()

        latest = df.iloc[-1]
        close = round_val(ticker, latest["Close"])
        sma20 = round_val(ticker, latest["SMA20"])
        sma50 = round_val(ticker, latest["SMA50"])

        # トレンド判定
        is_uptrend = (close > sma20) and (sma20 > sma50)
        vol_surge = latest["Volume"] > (latest["Vol20"] * (1.15 if is_jp else 1.20))

        if is_uptrend and vol_surge:
            stop_loss = round_val(ticker, df["Low"].tail(20).min())
            risk = close - stop_loss

            if risk > 0 and (risk / close) <= 0.08:
                target = round_val(ticker, close + (risk * 2.2))
                cur = "" if is_jp else "$"
                unit = "円" if is_jp else ""

                try:
                    info = stock.info
                    fund_ctx = f"PER:{info.get('forwardPE', 'N/A')}, 業種:{info.get('sector', '不明')}"
                except Exception:
                    fund_ctx = ""

                # ★ 新規追加: SEC一次開示と金融ニュースの同時取得
                sec_ctx = get_sec_recent_filings(ticker)
                news_ctx = get_stock_news(stock)

                ai_summary = analyze_with_gemini(
                    ticker,
                    fundamental_context=fund_ctx,
                    news_context=news_ctx,
                    sec_context=sec_ctx
                )

                jpy_tag = f" ({to_jpy_str(close)})" if not is_jp else ""
                buy_candidates.append(
                    f"🟢 {ticker}: {cur}{close}{unit}{jpy_tag}\n"
                    f"・目標: {cur}{target}{unit} / 撤退: {cur}{stop_loss}{unit}\n"
                    f"・AI: {ai_summary}"
                )
                if len(buy_candidates) >= 3:
                    break

    # ── C. 保有株の出口監視 ──
    hold_alerts = []
    total_cost_usd = 0.0
    total_value_usd = 0.0

    for ticker, data in MY_PORTFOLIO.items():
        stock = yf.Ticker(ticker)
        df = stock.history(period="3mo")
        if len(df) < 20:
            continue

        df["SMA20"] = df["Close"].rolling(20).mean()
        latest = df.iloc[-1]
        close = round_val(ticker, latest["Close"])
        sma20 = round_val(ticker, latest["SMA20"])

        shares = data["shares"]
        entry = data["entry"]

        cost = round(entry * shares, 1)
        value = round(close * shares, 1)
        diff = round(value - cost, 1)
        pnl_pct = round(((close - entry) / entry) * 100, 1)

        if not ticker.endswith(".T"):
            total_cost_usd += cost
            total_value_usd += value

        sign = "+" if diff >= 0 else ""

        # 1. 損切り (-7.5%)
        if pnl_pct <= -7.5:
            hold_alerts.append(
                f"🚨 {ticker} ({shares}株) 全売推奨\n"
                f"・取得: ${cost} ({to_jpy_str(cost)}) ➔ 現在: ${value} ({to_jpy_str(value)})\n"
                f"・損益: {sign}${diff} ({to_jpy_str(diff)}) [{sign}{pnl_pct}%]"
            )
        # 2. 20日線割れ
        elif close < sma20:
            action = "利確検討" if pnl_pct > 0 else "手仕舞い"
            hold_alerts.append(
                f"⚠️ {ticker} ({shares}株) {action}\n"
                f"・取得: ${cost} ({to_jpy_str(cost)}) ➔ 現在: ${value} ({to_jpy_str(value)})\n"
                f"・損益: {sign}${diff} ({to_jpy_str(diff)}) [{sign}{pnl_pct}%]"
            )

    # ── D. メッセージ組み立て ──
    sections = []

    if buy_candidates:
        sections.append("■ 注目米株（買い検討）\n" + "\n\n".join(buy_candidates))
    else:
        sections.append("■ 注目米株: 本日ブレイク条件合致なし")

    if hold_alerts:
        sections.append("■ 保有株アラート\n" + "\n\n".join(hold_alerts))

    if total_cost_usd > 0:
        total_pnl_usd = round(total_value_usd - total_cost_usd, 1)
        total_pnl_pct = round((total_pnl_usd / total_cost_usd) * 100, 1)
        sign = "+" if total_pnl_usd >= 0 else ""

        warn_note = "\n※SPY下落中（新規買いは少額に限定推奨）" if not market_is_bullish else "\n※地合い良好"
        summary = (
            f"💰 総資産: ${total_value_usd:,.1f} ({to_jpy_str(total_value_usd)}) | "
            f"損益: {sign}${total_pnl_usd:,.1f} ({to_jpy_str(total_pnl_usd)}) [{sign}{total_pnl_pct}%]"
            f"{warn_note}"
        )
        sections.append(summary)

    header = f"【米株・資産速報】1$ = {usdjpy}円\n\n"
    full_message = header + "\n\n━━━━━━━━━━\n\n".join(sections)

    send_line_message(full_message)
    print("✅ 一次情報・ニュース連携済みのLINE通知送信完了")


if __name__ == "__main__":
    run_trading_bot()
