import os
import json
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types

# Mega-Cap stocks, major ETFs, and USO with high options liquidity
WATCHLIST = ["NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "SPY", "QQQ", "USO"]

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version='v1beta')
)

def get_vix_level():
    """Fetches the latest VIX index closing/current value"""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if not hist.empty:
            return round(hist['Close'].iloc[-1], 2)
    except Exception as e:
        print(f"Error fetching VIX: {e}")
    return "N/A"

def get_already_sent_today():
    """Prevents duplicate alerts for the same ticker on the same day"""
    sent = set()
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        resp = requests.get(url, timeout=10).json()
        today_str = datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d")
        for item in resp.get("result", []):
            msg = item.get("message") or item.get("channel_post") or {}
            text = msg.get("text", "")
            msg_date = datetime.fromtimestamp(msg.get("date", 0), tz=ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m-%d")
            if msg_date == today_str:
                for t in WATCHLIST:
                    if f"[{t}]" in text:
                        sent.add(t)
    except Exception:
        pass
    return sent

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    resp = requests.post(url, json=payload)
    print("Telegram response:", resp.status_code, resp.text)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def check_daily_spread_strategy(ticker_symbol, vix_level):
    try:
        stock = yf.Ticker(ticker_symbol)
        hist = stock.history(period="1y", interval="1d")
        if len(hist) < 155:
            return None

        hist['SMA150'] = hist['Close'].rolling(150).mean()
        hist['RSI'] = calculate_rsi(hist['Close'], 14)
        hist['AvgVol20'] = hist['Volume'].rolling(20).mean()

        curr = hist.iloc[-1]
        prev = hist.iloc[-2]

        close_p = round(curr['Close'], 2)
        open_p = round(curr['Open'], 2)
        high_p = round(curr['High'], 2)
        low_p = round(curr['Low'], 2)
        sma_150 = round(curr['SMA150'], 2)
        rsi_curr = round(curr['RSI'], 1)
        rsi_prev = round(prev['RSI'], 1)
        vol_ratio = round(curr['Volume'] / curr['AvgVol20'], 2) if curr['AvgVol20'] > 0 else 1.0

        candle_range = high_p - low_p
        if candle_range == 0:
            return None
        close_position = (close_p - low_p) / candle_range  # 1.0 = daily high, 0.0 = daily low

        strategy_type = None

        # 1. Bull Put Spread check (6/6 bullish conditions above SMA 150)
        if (28 <= rsi_curr < 50) and (rsi_curr > rsi_prev) and (close_p > sma_150) and (close_p > open_p) and (vol_ratio >= 1.10) and (close_position >= 0.65):
            strategy_type = "BULL PUT SPREAD"

        # 2. Bear Call Spread check (6/6 bearish conditions below SMA 150)
        elif (50 < rsi_curr <= 72) and (rsi_curr < rsi_prev) and (close_p < sma_150) and (close_p < open_p) and (vol_ratio >= 1.10) and (close_position <= 0.35):
            strategy_type = "BEAR CALL SPREAD"

        if not strategy_type:
            return None

        # Condition 6: Filter out if earnings are within the
