import os
import requests
import yfinance as yf
import pandas as pd
from google import genai

# ── 1. API・アカウント設定（GitHub Secretsから安全に取得） ──
LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
USER_ID = "U97e66f5d50a462734d78a10e3e66f77b"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ── 2. 保有銘柄リスト（手仕舞い・損切り監視） ──
# 日本株を追加する場合は末尾に「.T」（例: "8306.T": {"shares": 100, "entry": 1600.0}）
MY_PORTFOLIO = {
    "NFLX": {"shares": 2, "entry": 86.91},
    "AMD":  {"shares": 1, "entry": 105.92},
    "NVDA": {"shares": 5, "entry": 117.38},
    "RBLX": {"shares": 10, "entry": 28.27},
    "JOBY": {"shares": 70, "entry": 6.55},
    "LUNR": {"shares": 10, "entry": 9.43}
}

# ── 3. 新規買い候補の監視リスト（日米ハイブリッド） ──
WATCH_UNIVERSE = {
    # 日本株：大型成長・半導体
    "JP_GROWTH": ["8035.T", "6857.T", "6758.T", "9984.T", "6501.T"],
    # 日本株：好業績バリュー・累進配当
    "JP_VALUE":  ["8306.T", "8058.T", "8591.T", "9432.T"],
    # 米国株：AI・メガテック
    "US_CORE":   ["NVDA", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "AVGO", "PLTR", "CRWD"],
    # 米国株：モメンタム・テーマ
    "US_BETA":   ["TSLA", "UBER", "APP", "ASTS", "COIN"]
}

# 全監視銘柄を平坦なリストに展開
ALL_WATCH_TICKERS = [ticker for group in WATCH_UNIVERSE.values() for ticker in group]


def send_line_message(text):
    """LINE Messaging API経由で通知をプッシュ送信"""
    if not LINE_ACCESS_TOKEN:
        print("LINE_ACCESS_TOKEN が設定されていません。")
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
    """Gemini 2.5 Flashで決算状況と直近材料を簡潔に要約"""
    if not GEMINI_API_KEY:
        return "Gemini APIキー未設定のためスキップ"

    prompt = f"""
あなたはスイングトレード専属アナリストです。
銘柄「{ticker_symbol}」について、投資判断に必要な最新情報を要約してください。

【前提データ】
{fundamental_context}

【確認項目】
1. 直近四半期決算の進捗とサプライズ（市場予想比）
2. 今後のガイダンスや市場予想
3. 直近の主な好材料または下落リスク

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
    buy_candidates = []
    
    # 通貨表記ヘルパー
    get_cur = lambda t: "円" if t.endswith(".T") else "$"
    round_val = lambda t, val: round(val, 0 if t.endswith(".T") else 2)

    # ── A. 地合い判定（S&P500の20日移動平均線） ──
    spy = yf.Ticker("SPY").history(period="3mo")
    spy["SMA20"] = spy["Close"].rolling(20).mean()
    market_is_bullish = spy["Close"].iloc[-1] > spy["SMA20"].iloc[-1]

    # ── B. 保有銘柄の出口監視 ──
    for ticker, data in MY_PORTFOLIO.items():
        stock = yf.Ticker(ticker)
        df = stock.history(period="3mo")
        if len(df) < 20:
            continue

        df["SMA20"] = df["Close"].rolling(20).mean()
        latest = df.iloc[-1]
        close = round_val(ticker, latest["Close"])
        sma20 = round_val(ticker, latest["SMA20"])
        entry = data["entry"]
        pnl_pct = round(((close - entry) / entry) * 100, 1)
        cur = get_cur(ticker)

        # 1. 買値から -7.5% での緊急損切りアラート
        if pnl_pct <= -7.5:
            alerts.append(
                f"🚨【緊急損切り推奨】{ticker}\n"
                f"現在値: {cur}{close} (取得比: {pnl_pct}%)\n"
                f"最大許容損失(-7.5%)に達しています。手仕舞いを推奨します。"
            )
        # 2. 20日線割れの手仕舞い・利益確定アラート
        elif close < sma20:
            alerts.append(
                f"⚠️【手仕舞い検討】{ticker}\n"
                f"現在値: {cur}{close} (損益: {pnl_pct}%)\n"
                f"20日線({cur}{sma20})を割り込みました。"
            )

    # ── C. 新規買い候補のスクリーニング ──
    if not market_is_bullish:
        alerts.insert(0, "🛡️【地合い警戒】S&P500が20日線を下回っています。新規エントリーは見合わせ推奨です。")
    else:
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

            # 上昇トレンド + 出来高急増（日本株は1.15倍、米国株は1.20倍）
            is_uptrend = (close > sma20) and (sma20 > sma50)
            vol_mult = 1.15 if is_jp else 1.20
            vol_surge = latest["Volume"] > (latest["Vol20"] * vol_mult)

            if is_uptrend and vol_surge:
                stop_loss = round_val(ticker, df["Low"].tail(20).min())
                risk = close - stop_loss

                # リスクが株価の8%以内
                if risk > 0 and (risk / close) <= 0.08:
                    target = round_val(ticker, close + (risk * 2.2))
                    cur = get_cur(ticker)

                    # Gemini用ファンダメンタルズ情報
                    try:
                        info = stock.info
                        context = (
                            f"セクター: {info.get('sector', '不明')}\n"
                            f"予想PER: {info.get('forwardPE', 'N/A')}\n"
                            f"配当利回り: {round(info.get('dividendYield', 0) * 100, 2)}%"
                        )
                        name = info.get("shortName", ticker) if is_jp else ticker
                    except Exception:
                        context = "ファンダメンタルズ詳細取得不可"
                        name = ticker

                    ai_summary = analyze_with_gemini(ticker, fundamental_context=context)

                    buy_candidates.append(
                        f"🟢【買い候補】{name} ({ticker})\n"
                        f"指値目安: {cur}{close}\n"
                        f"損切り: {cur}{stop_loss} (直近20日安値)\n"
                        f"利確目標: {cur}{target} (RR比 1:2.2)\n"
                        f"▼AI裏付けレポート\n{ai_summary}"
                    )

                    # 通知過多防止（最大3件まで）
                    if len(buy_candidates) >= 3:
                        break

    # ── D. LINE送信の組み立て ──
    message_sections = []
    if alerts:
        message_sections.append("■ 保有銘柄・地合いアラート\n" + "\n\n".join(alerts))
    if buy_candidates:
        message_sections.append("■ 新規スイング候補（AI精査済）\n" + "\n\n".join(buy_candidates))

    if message_sections:
        full_message = "【本日のAIトレードシグナル】\n\n" + "\n\n━━━━━━━━━━\n\n".join(message_sections)
        send_line_message(full_message)
        print("✅ LINE送信完了")
    else:
        print("本日通知するシグナルはありませんでした。")


if __name__ == "__main__":
    run_trading_bot()
