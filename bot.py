import json
import time
import threading
import logging
import websocket
import pandas as pd
from datetime import datetime
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== إعدادات (مباشرة للتجربة) ====================
TELEGRAM_TOKEN = "8585154138:AAFgKhUTg1qP5bGUBXqecSDeewHp3LwT-hU"
ADMIN_CHAT_ID = 5245111094

# بيانات جلسة Pocket Option التجريبية (حساب لا قيمة له)
PO_SESSION = "vtftn12e6f5f5008moitsd6skl"
PO_UID = 27658142
IS_DEMO = 1  # 1 = تجريبي

# أزواج فوركس حقيقية (بدون _otc)
SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "USDCAD", "USDCHF", "NZDUSD", "EURGBP"
]

# استراتيجية 5 دقائق
FAST_EMA = 50
SLOW_EMA = 200
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
SIGNAL_COOLDOWN = 120  # ثواني بين إشارات نفس الزوج

# ==================== متغيرات عامة ====================
price_buffers = {sym: deque(maxlen=500) for sym in SYMBOLS}
lock = threading.Lock()
signal_enabled = False
last_signal_times = {sym: 0 for sym in SYMBOLS}
app = None
ws = None

# ==================== حساب الإشارة ====================
def calculate_signal(symbol):
    with lock:
        prices = list(price_buffers[symbol])
    if len(prices) < max(SLOW_EMA, RSI_PERIOD, MACD_SLOW + MACD_SIGNAL) + 5:
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
    df['rsi'] = 100 - (100 / (1 + rs))

    ema_fast = df['close'].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = df['close'].ewm(span=MACD_SLOW, adjust=False).mean()
    df['macd'] = ema_fast - ema_slow
    df['macd_signal'] = df['macd'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    last = df.iloc[-1]
    prev = df.iloc[-2]

    uptrend = last['ema_fast'] > last['ema_slow']
    downtrend = last['ema_fast'] < last['ema_slow']

    # CALL
    if (uptrend and
        prev['rsi'] <= 30 and last['rsi'] > 30 and
        prev['macd_hist'] <= 0 and last['macd_hist'] > 0):
        return 'CALL', last['close']
    # PUT
    if (downtrend and
        prev['rsi'] >= 70 and last['rsi'] < 70 and
        prev['macd_hist'] >= 0 and last['macd_hist'] < 0):
        return 'PUT', last['close']
    return None

# ==================== WebSocket Pocket Option ====================
def on_message(ws, message):
    global signal_enabled
    try:
        if message.startswith("42"):
            payload = json.loads(message[2:])
            event = payload[0]
            if event == "price":
                data = payload[1]
                symbol = data[0]
                bid = float(data[1])
                ask = float(data[2])
                price = (bid + ask) / 2

                if symbol in price_buffers:
                    with lock:
                        price_buffers[symbol].append(price)

                    now = time.time()
                    if signal_enabled and (now - last_signal_times[symbol] > SIGNAL_COOLDOWN):
                        res = calculate_signal(symbol)
                        if res:
                            direction, curr_price = res
                            last_signal_times[symbol] = now
                            text = (f"📊 إشارة 5 دقائق:\n"
                                    f"الزوج: {symbol}\n"
                                    f"الاتجاه: {'شراء CALL' if direction == 'CALL' else 'بيع PUT'}\n"
                                    f"السعر: {curr_price:.5f}")
                            if app:
                                app.bot.send_message(ADMIN_CHAT_ID, text)
                            logging.info(f"إشارة {symbol}: {direction}")
    except Exception as e:
        logging.error(f"on_message error: {e}")

def on_error(ws, error):
    logging.error(f"WebSocket error: {error}")

def on_close(ws, status, msg):
    logging.warning("WebSocket closed. Reconnecting in 10s...")
    time.sleep(10)
    start_websocket()

def on_open(ws):
    # مصادقة
    auth_msg = f'42["auth",{{"session":"{PO_SESSION}","isDemo":{IS_DEMO},"uid":{PO_UID},"platform":2,"isFastHistory":true,"isOptimized":true}}]'
    ws.send(auth_msg)
    # اشتراك في الأزواج
    for sym in SYMBOLS:
        ws.send(f'42["subscribe",{{"symbol":"{sym}","timeframe":1}}]')
        time.sleep(0.1)
    logging.info("متصل بـ Pocket Option ومشترك في الأزواج")

def start_websocket():
    global ws
    ws_url = "wss://ws.pocketoption.com/socket.io/?EIO=4&transport=websocket"
    ws = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()

# ==================== بوت تيليجرام ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 بوت الإشارات متعدد الأزواج جاهز.\n"
                                    "/signal_on - تفعيل الإشارات\n"
                                    "/signal_off - إيقاف الإشارات\n"
                                    "/status - عرض الأسعار الحالية")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "مفعلة" if signal_enabled else "متوقفة"
    msg = f"الحالة: {state}\n"
    for sym in SYMBOLS:
        with lock:
            buf = price_buffers[sym]
            last = buf[-1] if buf else None
        msg += f"{sym}: {last:.5f}\n" if last else f"{sym}: ---\n"
    await update.message.reply_text(msg)

async def signal_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global signal_enabled
    signal_enabled = True
    await update.message.reply_text("✅ الإشارات مفعلة (5 دقائق)")

async def signal_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global signal_enabled
    signal_enabled = False
    await update.message.reply_text("⏸️ الإشارات متوقفة")

# ==================== تشغيل ====================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_websocket()
    time.sleep(2)

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("signal_on", signal_on))
    application.add_handler(CommandHandler("signal_off", signal_off))
    app = application
    application.run_polling()
