import os
import json
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from google import genai
from google.genai import types

# חברות ענק (Mega-Cap), מדדים מובילים ונפט (USO) עם נזילות אופציות גבוהה
WATCHLIST = ["NVDA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "TSLA", "AMD", "NFLX", "SPY", "QQQ", "USO"]

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version='v1beta')
)

def get_vix_level():
    """שולף את הערך העדכני של מדד הפחד VIX"""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="5d")
        if not hist.empty:
            return round(hist['Close'].iloc[-1], 2)
    except Exception as e:
        print(f"Error fetching VIX: {e}")
    return "לא זמין"

def get_already_sent_today():
    """מונע שליחת התראות כפולות על אותה מניה באותו יום"""
    sent = set()
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        resp = requests.get(url, timeout=10).json()
        today_str = datetime.now().strftime("%Y-%m-%d")
        for item in resp.get("result", []):
            msg = item.get("message") or item.get("channel_post") or {}
            text = msg.get("text", "")
            msg_date = datetime.fromtimestamp(msg.get("date", 0)).strftime("%Y-%m-%d")
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
        close_position = (close_p - low_p) / candle_range  # 1.0 = גבוה יומי, 0.0 = נמוך יומי

        strategy_type = None

        # 1. בדיקת Bull Put Spread (6/6 תנאים חיוביים מעל ממוצע 150)
        if (28 <= rsi_curr < 50) and (rsi_curr > rsi_prev) and (close_p > sma_150) and (close_p > open_p) and (vol_ratio >= 1.10) and (close_position >= 0.65):
            strategy_type = "BULL PUT SPREAD"

        # 2. בדיקת Bear Call Spread (6/6 תנאים שליליים מתחת לממוצע 150)
        elif (50 < rsi_curr <= 72) and (rsi_curr < rsi_prev) and (close_p < sma_150) and (close_p < open_p) and (vol_ratio >= 1.10) and (close_position <= 0.35):
            strategy_type = "BEAR CALL SPREAD"

        if not strategy_type:
            return None

        # תנאי 6: פסילה אם יש דו"ח כספי ב-5 הימים הקרובים (תעודות סל כמו SPY/QQQ/USO ידלגו בבטחה)
        today = datetime.now().date()
        earnings_str = "לא קרוב"
        try:
            cal = stock.calendar
            if cal is not None and 'Earnings Date' in cal:
                ed = cal['Earnings Date'][0]
                if hasattr(ed, 'date'):
                    ed = ed.date()
                days_to_ed = (ed - today).days
                if 0 <= days_to_ed <= 5:
                    print(f"Skipping {ticker_symbol}: Earnings in {days_to_ed} days")
                    return None
                earnings_str = f"{ed} (בעוד {days_to_ed} ימים)"
        except Exception:
            pass

        # בחירת פקיעה קצרה (1 עד 4 DTE) לעסקאות של 1-3 ימים
        expirations = stock.options
        target_exp = None
        dte = 0
        for exp in expirations:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            days = (exp_date - today).days
            if 1 <= days <= 4:
                target_exp = exp
                dte = days
                break

        if not target_exp:
            return None

        chain = stock.option_chain(target_exp)
        options_list = []

        if strategy_type == "BULL PUT SPREAD":
            puts = chain.puts
            filtered = puts[(puts['strike'] <= close_p * 0.99) & (puts['strike'] >= close_p * 0.93)]
            for _, row in filtered.iterrows():
                if row['ask'] > 0 and row['bid'] > 0:
                    options_list.append({
                        "strike": row['strike'],
                        "bid": row['bid'],
                        "ask": row['ask'],
                        "mid": round((row['bid'] + row['ask']) / 2, 2),
                        "iv": round(row['impliedVolatility'] * 100, 1),
                        "oi": int(row['openInterest']) if pd.notna(row['openInterest']) else 0
                    })
            options_list = options_list[-8:]
        else:
            calls = chain.calls
            filtered = calls[(calls['strike'] >= close_p * 1.01) & (calls['strike'] <= close_p * 1.07)]
            for _, row in filtered.iterrows():
                if row['ask'] > 0 and row['bid'] > 0:
                    options_list.append({
                        "strike": row['strike'],
                        "bid": row['bid'],
                        "ask": row['ask'],
                        "mid": round((row['bid'] + row['ask']) / 2, 2),
                        "iv": round(row['impliedVolatility'] * 100, 1),
                        "oi": int(row['openInterest']) if pd.notna(row['openInterest']) else 0
                    })
            options_list = options_list[:8]

        if len(options_list) < 2:
            return None

        return {
            "ticker": ticker_symbol,
            "strategy": strategy_type,
            "price": close_p,
            "sma_150": sma_150,
            "rsi_today": rsi_curr,
            "rsi_yesterday": rsi_prev,
            "volume_ratio": vol_ratio,
            "vix_index": vix_level,
            "next_earnings": earnings_str,
            "expiration": target_exp,
            "dte": dte,
            "option_chain_otm": options_list
        }
    except Exception as e:
        print(f"Error processing {ticker_symbol}: {e}")
        return None

def analyze_with_gemini(data):
    prompt = f"""
    You are an expert Options Credit Spread Trader (1-3 day hold) specializing in Bull Put Spreads and Bear Call Spreads.
    This asset just triggered all 6 Daily Dashboard conditions:
    {json.dumps(data, indent=2)}

    Rules for building the spread:
    1. If strategy is BULL PUT SPREAD: Select a Short Put strike roughly 1.5% to 3.5% below current price (preferably near/below SMA150 support), and a Long Put strike $1, $2.5, or $5 lower for protection (adjust width according to asset price, e.g., $1-$2 wide for lower-priced assets like USO, $5 wide for higher-priced stocks).
    2. If strategy is BEAR CALL SPREAD: Select a Short Call strike roughly 1.5% to 3.5% above current price (preferably near/above SMA150 resistance), and a Long Call strike $1, $2.5, or $5 higher for protection.
    3. Calculate Net Credit using Mid prices (Short Mid - Long Mid). Net Credit MUST be at least 20% of the spread width, and Open Interest must show good liquidity.
    4. If no valid liquid spread exists with >= 20% credit/width ratio, output ONLY: SKIP

    If APPROVED, output a crisp alert in Hebrew formatted for Telegram.
    CRITICAL: The very first line MUST include [{data['ticker']}] in square brackets so duplicate alerts are prevented!
    Format:
    [{data['ticker']}] {data['strategy']} (6/6 תנאים ביומי) | מחיר: ${data['price']}
    😱 מדד הפחד (VIX): {data['vix_index']}
    📊 נתוני הלוח: RSI יומי {data['rsi_today']} (אתמול {data['rsi_yesterday']}) | ממוצע 150: ${data['sma_150']} | ווליום פי {data['volume_ratio']}
    📅 פקיעה: {data['expiration']} ({data['dte']} DTE)
    🎯 מבנה הספרד:
    • מכירה (Short): סטרייק $[ShortStrike]
    • קנייה להגנה (Long): סטרייק $[LongStrike]
    💰 זיכוי נטו (רווח מקס'): $[NetCredit] ($[NetCredit*100] לחוזה) | סיכון מקס': $[MaxRisk*100]
    🛡️ ניהול סיכון: סגירת עסקה אם הנכס חוצה את $[StopLevel]
    """
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
    print("Starting Daily Credit Spread Scanner (Bull Put & Bear Call)...")
    vix_level = get_vix_level()
    print(f"Current VIX level: {vix_level}")
    already_sent = get_already_sent_today()
    for ticker in WATCHLIST:
        if ticker in already_sent:
            print(f"Skipping {ticker}: Already alerted today.")
            continue
        print(f"Scanning {ticker}...")
        data = check_daily_spread_strategy(ticker, vix_level)
        if data:
            print(f"6/6 Signal on {ticker} ({data['strategy']}), building spread with Gemini...")
            result = analyze_with_gemini(data)
            if "SKIP" not in result.upper():
                send_telegram(result)
    print("Scan finished.")

if __name__ == "__main__":
    main()
