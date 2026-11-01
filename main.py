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

# ── 2. 保有銘柄リスト（手仕舞い監視） ──
MY_PORTFOLIO = {
    "NFLX": {"shares": 2, "entry": 86.91},
    "AMD":  {"shares": 1, "entry": 105.92},
    "NVDA": {"shares": 5, "entry": 117.38},
    "RBLX": {"shares": 10, "entry": 28.27},
    "JOBY": {"shares": 70, "entry": 6.55},
    "LUNR": {"shares": 10, "entry": 9.43}
}

# ── 3. 新規買い候補の監視リスト ──
WATCH_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "PLTR",
    "CRWD", "PANW", "NET", "SNOW", "UBER", "COIN", "APP", "ASTS"
]

def send_line_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    payload = {"to": USER_ID, "messages": [{"type": "text", "text": text}]}
    requests.post(url, headers=headers, json=payload)

def analyze_with_gemini(ticker_symbol):
    prompt = f"""
あなたは米国株スイングトレードの専属アナリストです。
銘柄「{ticker_symbol}」について、投資判断に必要な最新情報を要約してください。

【確認項目】
1. 直近四半期決算（EPS・売上高は市場予想を上回ったか：Beat状況）
2. 通期ガイダンス（業績予想の修正や強弱）
3. 直近の主な材料（好材料または重大な規制・訴訟等のリスク）

【出力形式】
LINE通知用の簡潔な箇条書きで、日本語80〜120文字程度に凝縮してください。
冒頭に【決算評価】（好調/警戒/中立）を付けてください。
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"Gemini分析エラー: {str(e)}"

def run_trading_bot():
    alerts = []
    
    # A. 地合い判定（S&P500の20日線チェック）
    spy = yf.Ticker("SPY").history(period="3mo")
    spy["SMA20"] = spy["Close"].rolling(20).mean()
    market_is_bullish = spy["Close"].iloc[-1] > spy["SMA20"].iloc[-1]
    
    # B. 保有株の出口監視
    for ticker, data in MY_PORTFOLIO.items():
        stock = yf.Ticker(ticker)
        df = stock.history(period="3mo")
        if len(df) < 20:
            continue
        
        df["SMA20"] = df["Close"].rolling(20).mean()
        latest = df.iloc[-1]
        close = round(latest["Close"], 2)
        sma20 = round(latest["SMA20"], 2)
        entry = data["entry"]
        pnl_pct = round(((close - entry) / entry) * 100, 1)
        
        if close < sma20:
            alerts.append(
                f"⚠️【手仕舞い検討】{ticker}\n"
                f"現在値: ${close} (損益: {pnl_pct}%)\n"
                f"20日線(${sma20})を割り込みました。"
            )

    # C. 新規買いスクリーニング + Gemini要約
    buy_candidates = []
    if not market_is_bullish:
        alerts.insert(0, "🛡️【地合い警戒】S&P500が20日線を下回っています。新規エントリーは見合わせ推奨です。")
    else:
        for ticker in WATCH_UNIVERSE:
            stock = yf.Ticker(ticker)
            df = stock.history(period="6mo")
            if len(df) < 50:
                continue
                
            df["SMA20"] = df["Close"].rolling(20).mean()
            df["SMA50"] = df["Close"].rolling(50).mean()
            df["Vol20"] = df["Volume"].rolling(20).mean()
            
            latest = df.iloc[-1]
            close = round(latest["Close"], 2)
            sma20 = round(latest["SMA20"], 2)
            sma50 = round(latest["SMA50"], 2)
            
            is_uptrend = (close > sma20) and (sma20 > sma50)
            vol_surge = latest["Volume"] > latest["Vol20"] * 1.2
            
            if is_uptrend and vol_surge:
                stop_loss = round(df["Low"].tail(20).min(), 2)
                risk = close - stop_loss
                if risk > 0 and (risk / close) <= 0.08:
                    target = round(close + (risk * 2.2), 2)
                    ai_summary = analyze_with_gemini(ticker)
                    
                    buy_candidates.append(
                        f"🟢【買い候補】{ticker}\n"
                        f"指値目安: ${close}\n"
                        f"損切り: ${stop_loss} (直近支持線)\n"
                        f"利確目標: ${target} (RR比 1:2.2)\n"
                        f"▼AI裏付けレポート\n{ai_summary}"
                    )
                    if len(buy_candidates) >= 2:
                        break

    # D. LINE送信
    message_sections = []
    if alerts:
        message_sections.append("■ 保有銘柄・地合いアラート\n" + "\n\n".join(alerts))
    if buy_candidates:
        message_sections.append("■ 新規スイング候補（AI精査済）\n" + "\n\n".join(buy_candidates))
        
    if message_sections:
        full_message = "【本日のAIトレードシグナル】\n\n" + "\n\n━━━━━━━━━━\n\n".join(message_sections)
        send_line_message(full_message)

if __name__ == "__main__":
    run_trading_bot()
