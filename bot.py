import time
import threading
import logging
import requests
import pandas as pd
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== إعدادات ====================
TELEGRAM_TOKEN = "8585154138:AAFgKhUTg1qP5bGUBXqecSDeewHp3LwT-hU"
ADMIN_CHAT_ID = 5245111094
TWELVE_DATA_KEY = "460e31437f314674b7ce39412c9cb050"

SYMBOLS = ["EUR/USD", "GBP/USD"]   # زوجان رئيسيان
FETCH_INTERVAL = 60               # ثانية بين كل جلب
SIGNAL_COOLDOWN = 120             # ثانيتين بين إشارات نفس الزوج

FAST_EMA, SLOW_EMA = 50, 200
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# ==================== تخزين الأسعار ====================
price_buffers = {sym: deque(maxlen=500) for sym in SYMBOLS}
lock = threading.Lock()
signal_enabled = False
last_signal_times = {sym: 0 for sym in SYMBOLS}
app = None

def calculate_signal(symbol):
    with lock:
        prices = list(price_buffers[symbol])
    if len(prices) < max(SLOW_EMA, RSI_PERIOD, MACD_SLOW+MACD_SIGNAL)+5:
        return None
    df = pd.DataFrame(prices, columns=['close'])
    df['ema_fast'] = df['close'].ewm(span=FAST_EMA, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=SLOW_EMA, adjust=False).mean()
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100/(1+rs))
    ema_fast = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
    df['macd'] = ema_fast - ema_slow
    df['macd_signal'] = df['macd'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    last = df.iloc[-1]; prev = df.iloc[-2]
    uptrend = last['ema_fast'] > last['ema_slow']
    downtrend = last['ema_fast'] < last['ema_slow']
    if uptrend and prev['rsi']<=30 and last['rsi']>30 and prev['macd_hist']<=0 and last['macd_hist']>0:
        return 'CALL', last['close']
    if downtrend and prev['rsi']>=70 and last['rsi']<70 and prev['macd_hist']>=0 and last['macd_hist']<0:
        return 'PUT', last['close']
    return None

def fetch_loop():
    while True:
        for sym in SYMBOLS:
            try:
                url = f"https://api.twelvedata.com/price?symbol={sym}&apikey={TWELVE_DATA_KEY}"
                resp = requests.get(url).json()
                if 'price' in resp:
                    price = float(resp['price'])
                    with lock:
                        price_buffers[sym].append(price)
                    now = time.time()
                    if signal_enabled and (now - last_signal_times[sym] > SIGNAL_COOLDOWN):
                        res = calculate_signal(sym)
                        if res:
                            direction, cp = res
                            last_signal_times[sym] = now
                            app.bot.send_message(ADMIN_CHAT_ID,
                                f"📊 {sym}: {'شراء CALL' if direction=='CALL' else 'بيع PUT'} @ {cp:.5f}")
            except Exception as e:
                logging.error(f"Error {sym}: {e}")
        time.sleep(FETCH_INTERVAL)

# تيليجرام
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 بوت مؤقت (REST فقط)\n/signal_on /signal_off /status")
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "مفعلة" if signal_enabled else "متوقفة"
    msg = f"الحالة: {state}\n"
    for s in SYMBOLS:
        with lock: p = price_buffers[s][-1] if price_buffers[s] else None
        msg += f"{s}: {p:.5f}\n" if p else f"{s}: ---\n"
    await update.message.reply_text(msg)
async def signal_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global signal_enabled; signal_enabled = True
    await update.message.reply_text("✅ مفعلة (مؤقت)")
async def signal_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global signal_enabled; signal_enabled = False
    await update.message.reply_text("⏸️ متوقفة")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=fetch_loop, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, h in [("start", start_cmd), ("status", status), ("signal_on", signal_on), ("signal_off", signal_off)]:
        application.add_handler(CommandHandler(cmd, h))
    app = application
    application.run_polling()
