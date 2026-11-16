import os
import json
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from google import genai
from google.genai import types

# רשימת המניות לסריקה - תוכל להוסיף או להוריד מניות כאן בכל עת
WATCHLIST = ["NVDA", "META", "TSLA", "TSM", "AAPL", "AMD", "MSFT", "AMZN", "QQQ", "SPY"]

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# שימוש ב-v1beta מאפשר גישה לכל המודלים העדכניים של Gemini
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version='v1beta')
)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_candidate_data(ticker_symbol):
    try:
        stock = yf.Ticker(ticker_symbol)
        hist = stock.history(period="1y")
        if len(hist) < 200:
            return None

        current_price = round(hist['Close'].iloc[-1], 2)
        sma_50 = round(hist['Close'].rolling(50).mean().iloc[-1], 2)
        sma_200 = round(hist['Close'].rolling(200).mean().iloc[-1], 2)
        rsi = round(calculate_rsi(hist['Close']).iloc[-1], 1)

        if current_price < sma_200:
            return None
        if rsi > 48 and current_price > sma_50:
            return None

        today = datetime.now().date()
        earnings_date_str = "Unknown"
        try:
            cal = stock.calendar
            if cal is not None and 'Earnings Date' in cal:
                ed = cal['Earnings Date'][0]
                if hasattr(ed, 'date'):
                    ed = ed.date()
                earnings_date_str = str(ed)
                days_to_earnings = (ed - today).days
                if 0 <= days_to_earnings <= 42:
                    print(f"Skipping {ticker_symbol}: Earnings in {days_to_earnings} days")
                    return None
        except Exception:
            pass

        expirations = stock.options
        target_exp = None
        dte = 0
        for exp in expirations:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            days = (exp_date - today).days
            if 28 <= days <= 48:
                target_exp = exp
                dte = days
                break

        if not target_exp:
            return None

        chain = stock.option_chain(target_exp)
        puts = chain.puts
        otm_puts = puts[(puts['strike'] < current_price * 0.97) & (puts['strike'] > current_price * 0.80)]
        
        options_summary = []
        for _, row in otm_puts.iterrows():
            options_summary.append({
                "strike": row['strike'],
                "bid": row['bid'],
                "ask": row['ask'],
                "iv": round(row['impliedVolatility'] * 100, 1),
                "oi": row['openInterest']
            })

        if not options_summary:
            return None

        return {
            "ticker": ticker_symbol,
            "price": current_price,
            "sma_50": sma_50,
            "sma_200": sma_200,
            "rsi_14": rsi,
            "next_earnings": earnings_date_str,
            "expiration": target_exp,
            "dte": dte,
            "otm_puts": options_summary[-10:]
        }
    except Exception as e:
        print(f"Error processing {ticker_symbol}: {e}")
        return None

def analyze_with_gemini(data):
    prompt = f"""
    You are an expert Options Trader specializing in Bull Put Spreads.
    Analyze this stock candidate and its OTM Put option chain:
    {json.dumps(data, indent=2)}

    Rules for approval:
    1. Must be a solid technical pullback in an uptrend (Price > SMA200, RSI shows pullback).
    2. Select a Short Put strike roughly 5% to 10% below current price (approximating -0.25 to -0.30 delta) below clear support.
    3. Select a Long Put strike $5 or $10 wide (depending on stock price).
    4. Calculate Net Credit using Bid/Ask mid-prices. Net credit MUST be at least 22% of the spread width, and Open Interest must show decent liquidity.
    5. If the setup does NOT meet high-probability Bull Put Spread criteria, output ONLY the word: SKIP

    If APPROVED, write a crisp, professional alert in Hebrew formatted for Telegram (use *bold* for headers):
    - Ticker & Current Price + RSI
    - Expiration Date & DTE
    - Short Put Strike & Long Put Strike
    - Estimated Net Credit & Max Risk per contract
    - Brief 1-sentence technical rationale.
    """
    # מנסה אוטומטית את שמות המודלים העדכניים של Flash עד שאחד מצליח
    candidate_models = ["gemini-3-flash-preview", "gemini-3.0-flash", "gemini-2.5-flash"]
    last_error = None
    for model_name in candidate_models:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            last_error = e
            continue
    raise last_error

def main():
    print("Starting Bull Put Spread scan...")
    found_any = False
    for ticker in WATCHLIST:
        data = get_candidate_data(ticker)
        if data:
            print(f"Analyzing {ticker} with Gemini...")
            result = analyze_with_gemini(data)
            if "SKIP" not in result.upper():
                send_telegram(result)
                found_any = True
    
    if not found_any:
        print("Scan complete. No high-probability setups found today.")

if __name__ == "__main__":
    main()
