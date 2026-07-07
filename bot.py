import os, json, time, threading, logging
import requests, websocket, pandas as pd
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# إعدادات (ضع مفتاح Twelve Data مكان YOUR_API_KEY أو في متغيرات بيئة Render)
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "YOUR_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8585154138:AAFgKhUTg1qP5bGUBXqecSDeewHp3LwT-hU")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "5245111094"))

WS_SYMBOL = "EUR/USD"
REST_SYMBOLS = ["GBP/USD", "USD/JPY", "AUD/USD"]
ALL_SYMBOLS = [WS_SYMBOL] + REST_SYMBOLS
REST_INTERVAL = 600          # 10 دقائق
SIGNAL_COOLDOWN = 120
FAST_EMA, SLOW_EMA = 50, 200
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

price_buffers = {sym: deque(maxlen=500) for sym in ALL_SYMBOLS}
lock = threading.Lock()
signal_enabled = False
last_signal_times = {sym: 0 for sym in ALL_SYMBOLS}
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

# WebSocket للرمز الرئيسي
def on_message(ws, message):
    global signal_enabled
    try:
        data = json.loads(message)
        if data.get('event')=='price':
            price = (float(data['bid'])+float(data['ask']))/2
            with lock:
                price_buffers[WS_SYMBOL].append(price)
            now = time.time()
            if signal_enabled and (now - last_signal_times[WS_SYMBOL] > SIGNAL_COOLDOWN):
                res = calculate_signal(WS_SYMBOL)
                if res:
                    direction, cp = res
                    last_signal_times[WS_SYMBOL] = now
                    app.bot.send_message(ADMIN_CHAT_ID, f"📊 {WS_SYMBOL}: {'CALL' if direction=='CALL' else 'PUT'} @ {cp:.5f}")
    except Exception as e:
        logging.error(f"WS msg: {e}")

def on_open(ws):
    ws.send(json.dumps({"action":"subscribe","params":{"symbols":WS_SYMBOL}}))
def on_error(ws, error): logging.error(f"WS error: {error}")
def on_close(ws, status, msg):
    time.sleep(10)
    start_ws()

def start_ws():
    ws = websocket.WebSocketApp(
        f"wss://ws.twelvedata.com/v1/quotes/price?apikey={TWELVE_DATA_KEY}",
        on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
    threading.Thread(target=ws.run_forever, daemon=True).start()

# REST لباقي الأزواج
def fetch_rest():
    while True:
        for sym in REST_SYMBOLS:
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
                            app.bot.send_message(ADMIN_CHAT_ID, f"📊 {sym}: {'CALL' if direction=='CALL' else 'PUT'} @ {cp:.5f}")
            except Exception as e:
                logging.error(f"REST {sym}: {e}")
        time.sleep(REST_INTERVAL)

# تيليجرام
async def start_cmd(update, context):
    await update.message.reply_text("جاهز /signal_on /signal_off /status")
async def status(update, context):
    state = "مفعلة" if signal_enabled else "متوقفة"
    msg = f"{state}\n"
    for s in ALL_SYMBOLS:
        with lock: p = price_buffers[s][-1] if price_buffers[s] else None
        msg += f"{s}: {p:.5f}\n" if p else f"{s}: ---\n"
    await update.message.reply_text(msg)
async def signal_on(update, context):
    global signal_enabled; signal_enabled = True
    await update.message.reply_text("✅ مفعلة")
async def signal_off(update, context):
    global signal_enabled; signal_enabled = False
    await update.message.reply_text("⏸️ متوقفة")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_ws()
    threading.Thread(target=fetch_rest, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, h in [("start", start_cmd), ("status", status), ("signal_on", signal_on), ("signal_off", signal_off)]:
        application.add_handler(CommandHandler(cmd, h))
    app = application
    application.run_polling()
