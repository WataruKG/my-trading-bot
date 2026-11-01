import os
import requests
import yfinance as yf
import pandas as pd
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

# ── 3. 新規買い候補の監視リスト（米国株中心に拡充） ──
WATCH_UNIVERSE = {
    # 米国：大型AI・メガテック主力
    "US_MEGA": [
        "NVDA", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "AVGO", "TSLA"
    ],
    # 米国：セキュリティ・SaaS・クラウド
    "US_SAAS": [
        "PLTR", "CRWD", "PANW", "NET", "SNOW", "MDB", "DDOG", "SHOP"
    ],
    # 米国：半導体・AIインフラ・電力
    "US_HARDWARE_INFRA": [
        "ARM", "MRVL", "VST"
    ],
    # 米国：高モメンタム・急成長・テーマ（フィンテック / 宇宙 / ヘルスケア）
    "US_MOMENTUM": [
        "APP", "ASTS", "COIN", "UBER", "HOOD", "RDDT", "RKLB", "LLY"
    ],
    # 日本株（参考監視）
    "JP_REF": [
        "8035.T", "6857.T", "8306.T"
    ]
}

# 全監視銘柄を平坦なリストに展開
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


def analyze_with_gemini(ticker_symbol, fundamental_context=""):
    """超短縮・要点のみ要約（トークン節約＆文字数圧縮）"""
    if not GEMINI_API_KEY:
        return "AI未設定"

    prompt = f"""
米国株スイングアナリストとして「{ticker_symbol}」の最新状況を要約。
前提: {fundamental_context}

条件:
・日本語50〜70文字厳守
・冒頭に【好調】【警戒】【中立】のいずれかを付与
・直近決算と主要材料の核心のみ記述
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
    """ドル円レート取得"""
    try:
        usdjpy = yf.Ticker("USDJPY=X").history(period="5d")
        return round(usdjpy["Close"].iloc[-1], 1)
    except Exception:
        return 155.0


def run_trading_bot():
    usdjpy = get_usdjpy_rate()

    # 円表記略縮ヘルパー（万単位表記で短縮）
    def to_jpy_str(usd_val):
        jpy = usd_val * usdjpy
        if abs(jpy) >= 10000:
            return f"約{jpy/10000:.1f}万"
        return f"約{round(jpy):,}円"

    round_val = lambda t, val: round(val, 0 if t.endswith(".T") else 1)

    # ── A. 地合いチェック（S&P500） ──
    spy = yf.Ticker("SPY").history(period="3mo")
    spy["SMA20"] = spy["Close"].rolling(20).mean()
    market_is_bullish = spy["Close"].iloc[-1] > spy["SMA20"].iloc[-1]

    # ── B. 新規買い候補（米国株中心にスキャン） ──
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
                    context = f"PER:{info.get('forwardPE', 'N/A')}, RevGrow:{info.get('revenueGrowth', 'N/A')}"
                except Exception:
                    context = ""

                ai_summary = analyze_with_gemini(ticker, fundamental_context=context)

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

    # 1. 新規候補（最優先）
    if buy_candidates:
        sections.append("■ 注目米株（買い検討）\n" + "\n\n".join(buy_candidates))
    else:
        sections.append("■ 注目米株: 本日ブレイク条件合致なし")

    # 2. 保有株アラート
    if hold_alerts:
        sections.append("■ 保有株アラート\n" + "\n\n".join(hold_alerts))

    # 3. 合計サマリー（1行集約）
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
    print("✅ 拡張版LINE通知送信完了")


if __name__ == "__main__":
    run_trading_bot()
