import time
import threading
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from collections import deque
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== إعدادات ====================
TELEGRAM_TOKEN = "8585154138:AAFgKhUTg1qP5bGUBXqecSDeewHp3LwT-hU"
ADMIN_CHAT_ID = 5245111094
TWELVE_DATA_KEY = "460e31437f314674b7ce39412c9cb050"

SYMBOLS = ["EUR/USD", "GBP/USD"]       # زوجان رئيسيان
FETCH_INTERVAL = 60                   # ثانية بين الجلب
SIGNAL_COOLDOWN = 120                 # ثانيتين بين إشارات نفس الزوج
TRADE_DURATION_MINUTES = 5            # مدة الصفقة

# استراتيجية 5 دقائق
FAST_EMA, SLOW_EMA = 50, 200
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# ==================== فلتر الأخبار (قائمة ثابتة) ====================
# الأوقات بتوقيت UTC. سنضيف بعض الأحداث القوية الشهرية/الأسبوعية.
# يمكنك تعديل هذه القائمة أو إضافة المزيد.
# الصيغة: (شهر, يوم_من_الشهر_أو_يوم_أسبوع, ساعة, دقيقة, مدة_الحظر_بالدقائق)
# مثال: Non-Farm Payrolls أول جمعة من الشهر الساعة 13:30 UTC، الحظر 30 دقيقة قبل وبعد.
NEWS_EVENTS = [
    # (الشهر (1-12), (None=كل شهر, أو رقم محدد), (None=كل يوم, أو رقم), ساعة, دقيقة, مدة الحظر قبل/بعد بالدقائق)
    # Non-Farm Payrolls (أول جمعة)
    ("first_friday", 13, 30, 45),     # حظر 45 دقيقة حولها
    # قرار الفائدة الفيدرالي (تقريبي، يمكن ضبطه)
    ("third_wednesday", 18, 0, 60),   # كل رابع أربعاء؟ سنستخدم تقريب
    # مؤشر أسعار المستهلك CPI (شهرياً، بين 10-15 من الشهر 13:30 UTC)
    ("monthly", 10, 13, 30, 30),      # تقريب، سيتم التعديل
]

def is_news_time():
    """التحقق مما إذا كان الوقت الحالي ضمن فترة حظر أخبار"""
    now_utc = datetime.utcnow()
    weekday = now_utc.weekday()  # 0=Monday
    day = now_utc.day
    month = now_utc.month
    hour = now_utc.hour
    minute = now_utc.minute

    for event in NEWS_EVENTS:
        event_type = event[0]
        event_hour = event[1]
        event_minute = event[2]
        block_minutes = event[3]

        # حساب وقت بداية الحدث
        event_time = None
        if event_type == "first_friday":
            # أول جمعة من الشهر
            if weekday == 4 and day <= 7:  # أول جمعة
                event_time = datetime(now_utc.year, month, day, event_hour, event_minute)
        elif event_type == "third_wednesday":
            # ثالث أربعاء من الشهر (أيام 15-21)
            if weekday == 2 and 15 <= day <= 21:
                event_time = datetime(now_utc.year, month, day, event_hour, event_minute)
        elif event_type == "monthly":
            # يوم 10 من كل شهر مثلاً
            if day == event[2]:  # event[2] هو يوم الشهر
                event_time = datetime(now_utc.year, month, day, event_hour, event_minute)

        if event_time:
            # حساب الفرق بالدقائق
            diff = abs((now_utc - event_time).total_seconds()) / 60
            if diff <= block_minutes:
                return True
    return False

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
                        # فلتر الأخبار
                        if is_news_time():
                            logging.info(f"إشارة ممنوعة بسبب وقت أخبار: {sym}")
                            continue

                        res = calculate_signal(sym)
                        if res:
                            direction, cp = res
                            last_signal_times[sym] = now_ts
                            # تحويل التوقيت إلى صنعاء (UTC+3)
                            sanaa_time = datetime.utcnow() + timedelta(hours=3)
                            time_str = sanaa_time.strftime("%Y-%m-%d %H:%M:%S")
                            text = (
                                f"📊 إشارة صفقة 5 دقائق\n"
                                f"الزوج: {sym}\n"
                                f"الاتجاه: {'شراء CALL' if direction == 'CALL' else 'بيع PUT'}\n"
                                f"السعر الحالي: {cp:.5f}\n"
                                f"توقيت صنعاء: {time_str}\n"
                                f"يمكن الدخول الآن قبل اكتمال الشمعة"
                            )
                            app.bot.send_message(ADMIN_CHAT_ID, text)
                            logging.info(f"إشارة {sym}: {direction} @ {cp:.5f}")
            except Exception as e:
                logging.error(f"Error {sym}: {e}")
        time.sleep(FETCH_INTERVAL)

# تيليجرام
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت الإشارات (5 دقائق) - توقيت صنعاء\n"
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

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=fetch_loop, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, h in [("start", start_cmd), ("status", status), ("signal_on", signal_on), ("signal_off", signal_off)]:
        application.add_handler(CommandHandler(cmd, h))
    app = application
    application.run_polling()
