import os
import time
import requests
import psycopg2
import ccxt
import numpy as np

# ==================== الإعدادات والمتغيرات ====================
API_KEY = os.getenv("MEXC_API_KEY", "").strip()
API_SECRET = os.getenv("MEXC_API_SECRET", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_URL = os.getenv("DATABASE_URL", "").strip()

WATCHLIST = [
    'SOL/USDT', 'ETH/USDT', 'DOGE/USDT', 
    'NEAR/USDT', 'AVAX/USDT', 'SUI/USDT', 'LINK/USDT'
]

MAX_SLOTS = 4                 # 4 أزواج متزامنة كحد أقصى
SLOT_PERCENTAGE = 0.25        # 25% من إجمالي المحفظة لكل زوج
GRID_LEVELS = 3               # تقسيم الحصة إلى 3 أوامر Limit
GRID_STEP_PERCENT = 0.002     # فارق 0.2% بين كل أمر شراء
TP_PERCENT = 0.0035           # هدف جني ربح 0.35% (أمر بيع Limit بصفر رسوم)

exchange = ccxt.mexc({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'spot',
        'createMarketBuyOrderRequiresPrice': False
    }
})
exchange.apiKey = API_KEY
exchange.secret = API_SECRET

# ==================== قاعدة البيانات ====================
def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    status VARCHAR(20) DEFAULT 'OPEN',
                    buy_order_id VARCHAR(64),
                    sell_order_id VARCHAR(64),
                    buy_price NUMERIC(14, 4) NOT NULL,
                    sell_price NUMERIC(14, 4),
                    quantity NUMERIC(14, 4) NOT NULL,
                    cost_usd NUMERIC(14, 4) NOT NULL,
                    net_profit_usd NUMERIC(14, 4) DEFAULT 0.0,
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
            """)
        conn.commit()

# ==================== تيليجرام ====================
def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"Telegram error: {err}")

# ==================== حساب مؤشر البولنجر باند ====================
def get_bollinger_bands(symbol: str, period: int = 20, num_std: float = 2.0):
    """جلب بيانات شموع دقيقة واحدة وحساب Bollinger Bands"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=period + 5)
        if len(ohlcv) < period:
            return None, None, None
        
        closes = np.array([c[4] for c in ohlcv[-period:]], dtype=float)
        sma = float(np.mean(closes))
        std = float(np.std(closes))
        lower_band = sma - (num_std * std)
        current_close = float(closes[-1])
        return lower_band, sma, current_close
    except Exception as err:
        print(f"خطأ جلب بيانات الشموع لـ {symbol}: {err}")
        return None, None, None

# ==================== تصفية البداية ====================
def clean_start_flush():
    """تصفية العملات المتبقية لبدء الحساب نقدياً بنسبة 100%"""
    try:
        balance = exchange.fetch_balance()
        for symbol in WATCHLIST:
            base = symbol.split('/')[0]
            qty = float(balance['free'].get(base, 0.0))
            if qty > 0:
                ticker = exchange.fetch_ticker(symbol)
                cur_price = float(ticker['last'])
                if (qty * cur_price) > 5.0:  # بيع الكميات التي تتجاوز قيمتها 5$
                    exchange.create_market_sell_order(symbol, qty)
                    print(f"تمت تصفية {qty} من {base}")

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE trades SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP WHERE status = 'OPEN';")
            conn.commit()

        send_telegram("🧹 *تم إفراغ المراكز السابقة وتجهيز الرصيد كاش بنجاح.*\nالنظام يبدأ مسح شبكة البولنجر اللحظية.")
    except Exception as err:
        print(f"تنبيه أثناء التصفية: {err}")

# ==================== محرك التداول الأساسي ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("خطأ فادح: مفاتيح المنصة غير معرفة.")
        return

    init_db()
    clean_start_flush()

    send_telegram(
        f"⚡ *تم إطلاق نظام Bollinger Micro-Grid الهجين*\n"
        f"• نوع الأوامر: `Maker Limit (صفر رسوم)`\n"
        f"• الدخول: `عند كسر النطاق السفلي للبولنجر (1m)`\n"
        f"• التوزيع: `25% لكل مركز مقسمة لـ 3 أوامر شبكية`\n"
        f"• الربح اللحظي المستهدف: `+0.35% لكل مستوى`"
    )

    active_pairs = set()

    while True:
        try:
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get('USDT', 0.0))
            total_equity = float(balance['total'].get('USDT', 0.0))
            slot_size = total_equity * SLOT_PERCENTAGE

            # 1. متابعة الصفقات المفتوحة في قاعدة البيانات
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, symbol, sell_order_id, quantity, buy_price, cost_usd FROM trades WHERE status = 'OPEN';")
                    open_trades = cur.fetchall()

            for trade in open_trades:
                t_id, t_symbol, sell_id, t_qty, b_price, cost = trade[0], trade[1], trade[2], float(trade[3]), float(trade[4]), float(trade[5])
                
                # فحص تنفيذ أمر البيع Limit
                if sell_id:
                    try:
                        order_info = exchange.fetch_order(sell_id, t_symbol)
                        if order_info['status'] == 'closed':
                            exit_price = float(order_info.get('average') or (b_price * (1 + TP_PERCENT)))
                            profit = (exit_price - b_price) * t_qty

                            with get_db_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE trades 
                                        SET status = 'CLOSED', sell_price = %s, net_profit_usd = %s, closed_at = CURRENT_TIMESTAMP
                                        WHERE id = %s;
                                    """, (exit_price, profit, t_id))
                                conn.commit()

                            send_telegram(
                                f"🎯 *جني ربح شبكي مكتمل (#{t_id})*\n"
                                f"• الزوج: `{t_symbol}`\n"
                                f"• سعر البيع: `{exit_price:.4f}$`\n"
                                f"• الربح الصافي: `+{profit:.2f} USDT` (0% رسوم صانع)"
                            )
                            if t_symbol in active_pairs:
                                active_pairs.discard(t_symbol)
                    except Exception as e:
                        pass

            # 2. مسح الأزواج والبحث عن كسر البولنجر السفلي
            if len(active_pairs) < MAX_SLOTS and usdt_free >= slot_size and slot_size >= 20.0:
                for symbol in WATCHLIST:
                    if symbol in active_pairs:
                        continue

                    lower_band, sma, cur_close = get_bollinger_bands(symbol)
                    if not lower_band:
                        continue

                    # شرط الدخول: شمعة الدقيقة كسرت أو عادلت الحد السفلي
                    if cur_close <= lower_band:
                        active_pairs.add(symbol)
                        send_telegram(f"📉 *إشارة قاع مكتشفة على {symbol}*\nالسعر `{cur_close:.4f}` تحت الحد السفلي للبولنجر `{lower_band:.4f}`. بدء نشر أوامر Limit...")

                        level_size_usd = slot_size / GRID_LEVELS
                        for i in range(GRID_LEVELS):
                            buy_limit_price = cur_close * (1 - (i * GRID_STEP_PERCENT))
                            buy_limit_price = float(exchange.price_to_precision(symbol, buy_limit_price))
                            qty = level_size_usd / buy_limit_price
                            qty = float(exchange.amount_to_precision(symbol, qty))

                            # وضع أمر الشراء Limit كصانع سوق (0% رسوم)
                            try:
                                buy_order = exchange.create_limit_buy_order(symbol, qty, buy_limit_price)
                                b_id = buy_order['id']

                                # وضع أمر بيع Limit المقابل مباشرة لجني الربح فور الشراء
                                sell_limit_price = buy_limit_price * (1 + TP_PERCENT)
                                sell_limit_price = float(exchange.price_to_precision(symbol, sell_limit_price))
                                
                                # انتظار لحظي لتسجيل أمر البيع بمجرد التنفيذ
                                with get_db_connection() as conn:
                                    with conn.cursor() as cur:
                                        cur.execute("""
                                            INSERT INTO trades (symbol, status, buy_order_id, buy_price, quantity, cost_usd)
                                            VALUES (%s, 'OPEN', %s, %s, %s, %s);
                                        """, (symbol, b_id, buy_limit_price, qty, buy_limit_price * qty))
                                    conn.commit()

                            except Exception as order_err:
                                print(f"خطأ في وضع أوامر الشبكة لـ {symbol}: {order_err}")

                        if len(active_pairs) >= MAX_SLOTS:
                            break

            time.sleep(7)

        except Exception as loop_err:
            print(f"خطأ في الدورة: {loop_err}")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
