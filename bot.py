import os
import time
import threading
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from collections import deque
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== إعدادات ====================
TELEGRAM_TOKEN = "8585154138:AAFgKhUTg1qP5bGUBXqecSDeewHp3LwT-hU"
ADMIN_CHAT_ID = 5245111094
TWELVE_DATA_KEY = "460e31437f314674b7ce39412c9cb050"

SYMBOLS = ["EUR/USD", "GBP/USD"]
FETCH_INTERVAL = 60               # ثانية بين كل جلب
SIGNAL_COOLDOWN = 120             # ثانيتين بين إشارات الزوج نفسه
TRADE_DURATION_MINUTES = 5

# استراتيجية 5 دقائق
FAST_EMA, SLOW_EMA = 50, 200
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# ==================== فلتر الأخبار ====================
NEWS_EVENTS = [
    # (النمط, الساعة, الدقيقة, دقائق الحظر حول الحدث)
    ("first_friday", 13, 30, 45),        # التوظيف غير الزراعي (أول جمعة)
    ("third_wednesday", 18, 0, 60),      # قرار الفائدة (تقديري)
    ("monthly", 10, 13, 30, 30),         # مؤشر أسعار المستهلك (يوم 10 من الشهر)
]

def is_news_time():
    """التحقق مما إذا كان الوقت الحالي ضمن فترة حظر أخبار"""
    now_utc = datetime.now(datetime.UTC)  # إصلاح التحذير
    weekday = now_utc.weekday()           # 0=الإثنين
    day = now_utc.day
    month = now_utc.month
    hour = now_utc.hour
    minute = now_utc.minute

    for event in NEWS_EVENTS:
        event_type, event_hour, event_minute, block_minutes = event
        event_time = None

        if event_type == "first_friday":
            if weekday == 4 and day <= 7:      # أول جمعة من الشهر
                event_time = datetime(now_utc.year, month, day, event_hour, event_minute)
        elif event_type == "third_wednesday":
            if weekday == 2 and 15 <= day <= 21: # ثالث أربعاء
                event_time = datetime(now_utc.year, month, day, event_hour, event_minute)
        elif event_type == "monthly":
            if day == event[2]:                  # يوم محدد من الشهر (10)
                event_time = datetime(now_utc.year, month, day, event_hour, event_minute)

        if event_time:
            diff = abs((now_utc - event_time).total_seconds()) / 60
            if diff <= block_minutes:
                return True
    return False

# ==================== تخزين الأسعار ====================
price_buffers = {sym: deque(maxlen=500) for sym in SYMBOLS}
lock = threading.Lock()
signal_enabled = False
last_signal_times = {sym: 0 for sym in SYMBOLS}
bot_app = None  # سيُملأ لاحقاً

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

    if uptrend and prev['rsi'] <= 30 and last['rsi'] > 30 and prev['macd_hist'] <= 0 and last['macd_hist'] > 0:
        return 'CALL', last['close']
    if downtrend and prev['rsi'] >= 70 and last['rsi'] < 70 and prev['macd_hist'] >= 0 and last['macd_hist'] < 0:
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

                    now_ts = time.time()
                    if signal_enabled and (now_ts - last_signal_times[sym] > SIGNAL_COOLDOWN):
                        if is_news_time():
                            logging.info(f"إشارة ممنوعة بسبب الأخبار: {sym}")
                            continue

                        res = calculate_signal(sym)
                        if res:
                            direction, cp = res
                            last_signal_times[sym] = now_ts
                            sanaa_time = datetime.now(datetime.UTC) + timedelta(hours=3)
                            time_str = sanaa_time.strftime("%Y-%m-%d %H:%M:%S")
                            text = (
                                f"📊 إشارة صفقة 5 دقائق\n"
                                f"الزوج: {sym}\n"
                                f"الاتجاه: {'شراء CALL' if direction == 'CALL' else 'بيع PUT'}\n"
                                f"السعر الحالي: {cp:.5f}\n"
                                f"توقيت صنعاء: {time_str}\n"
                                f"يمكن الدخول الآن قبل اكتمال الشمعة"
                            )
                            bot_app.bot.send_message(ADMIN_CHAT_ID, text)
                            logging.info(f"إشارة {sym}: {direction} @ {cp:.5f}")
            except Exception as e:
                logging.error(f"خطأ {sym}: {e}")
        time.sleep(FETCH_INTERVAL)

# ==================== بوت تيليجرام ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت إشارات 5 دقائق - توقيت صنعاء\n"
        "/signal_on - تفعيل الإشارات\n"
        "/signal_off - إيقاف الإشارات\n"
        "/status - الأسعار الحالية"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = "مفعلة" if signal_enabled else "متوقفة"
    msg = f"الحالة: {state}\n"
    for s in SYMBOLS:
        with lock:
            p = price_buffers[s][-1] if price_buffers[s] else None
        msg += f"{s}: {p:.5f}\n" if p else f"{s}: ---\n"
    await update.message.reply_text(msg)

async def signal_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global signal_enabled
    signal_enabled = True
    await update.message.reply_text("✅ الإشارات مفعلة (يتم الفلترة أثناء الأخبار)")

async def signal_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global signal_enabled
    signal_enabled = False
    await update.message.reply_text("⏸️ الإشارات متوقفة")

# ==================== خادم Flask (لاجتياز فحص المنفذ) ====================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    print(f"Starting Flask on port {port}...")
    # use_reloader=False ضروري لتجنب تشغيل الخادم مرتين
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ==================== التشغيل ====================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 1. تشغيل Flask في خيط منفصل
    t_flask = Thread(target=run_web_server)
    t_flask.start()

    # 2. إعطاء Flask لحظة للنهوض (حتى يستجيب لفحص Render)
    time.sleep(2)

    # 3. تشغيل خيط جمع البيانات والإشارات
    t_fetch = Thread(target=fetch_loop, daemon=True)
    t_fetch.start()

    # 4. تشغيل بوت تيليجرام (Polling)
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, h in [("start", start_cmd), ("status", status), ("signal_on", signal_on), ("signal_off", signal_off)]:
        application.add_handler(CommandHandler(cmd, h))
    bot_app = application
    application.run_polling()
