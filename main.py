import os
import time
import requests
import psycopg2
import ccxt
from urllib.parse import urlparse

# قراءة المتغيرات
API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_URL = os.getenv("DATABASE_URL")

exchange = ccxt.mexc({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

SYMBOL = 'SOL/USDT'
BASE_COIN = 'SOL'
QUOTE_COIN = 'USDT'
TRADE_SIZE_USD = 100.0
TAKE_PROFIT_RATIO = 0.009

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
                    buy_price NUMERIC(14, 4) NOT NULL,
                    sell_price NUMERIC(14, 4),
                    quantity NUMERIC(14, 4) NOT NULL,
                    net_profit_usd NUMERIC(14, 4) DEFAULT 0.0,
                    cumulative_profit NUMERIC(14, 4) DEFAULT 0.0,
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
            """)
        conn.commit()

def get_cumulative_profit():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(net_profit_usd), 0.0) FROM trades WHERE status = 'CLOSED';")
            return float(cur.fetchone()[0])

def check_open_position():
    """استرجاع أي صفقة كانت معلقة قبل إعادة تشغيل السيرفر"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, buy_price, quantity 
                FROM trades 
                WHERE status = 'OPEN' AND symbol = %s 
                ORDER BY id DESC LIMIT 1;
            """, (SYMBOL,))
            row = cur.fetchone()
            if row:
                return {"id": row[0], "buy_price": float(row[1]), "qty": float(row[2])}
    return None

def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"خطأ تيليجرام: {err}")

def run_bot():
    init_db()
    current_pos = check_open_position()
    
    if current_pos:
        send_telegram(f"⚠️ *تم رصد صفقة مفتوحة سابقة!*\nتم استعادة المركز تلقائياً عند سعر دخول: `{current_pos['buy_price']}$`")
    else:
        send_telegram(f"🚀 *تم بدء تشغيل البوت بنجاح ومزامنته مع قاعدة البيانات*\nالرصيد المخصص للصفقة: `{TRADE_SIZE_USD}$`")

    while True:
        try:
            ticker = exchange.fetch_ticker(SYMBOL)
            current_price = float(ticker['last'])

            # فحص المركز الحالي
            if not current_pos:
                balance = exchange.fetch_balance()
                usdt_avail = float(balance['free'].get(QUOTE_COIN, 0.0))

                if usdt_avail >= TRADE_SIZE_USD:
                    qty = round(TRADE_SIZE_USD / current_price, 2)
                    order = exchange.create_market_buy_order(SYMBOL, qty)
                    entry_price = float(order.get('average') or current_price)

                    # حفظ الصفقة في قاعدة البيانات بحالة OPEN
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO trades (symbol, status, buy_price, quantity)
                                VALUES (%s, 'OPEN', %s, %s) RETURNING id;
                            """, (SYMBOL, entry_price, qty))
                            trade_id = cur.fetchone()[0]
                        conn.commit()

                    current_pos = {"id": trade_id, "buy_price": entry_price, "qty": qty}
                    target_tp = entry_price * (1 + TAKE_PROFIT_RATIO)

                    send_telegram(
                        f"🟢 *صفقة جديدة مسجلة برقم (#{trade_id})*\n"
                        f"• العملة: `{SYMBOL}`\n"
                        f"• سعر الشراء: `{entry_price:.2f}$`\n"
                        f"• الهدف: `{target_tp:.2f}$`"
                    )

            else:
                # مراقبة الهدف للصفقة المفتوحة
                target_price = current_pos['buy_price'] * (1 + TAKE_PROFIT_RATIO)
                if current_price >= target_price:
                    balance = exchange.fetch_balance()
                    sol_avail = float(balance['free'].get(BASE_COIN, 0.0))
                    sell_qty = min(current_pos['qty'], sol_avail)

                    if sell_qty > 0:
                        order = exchange.create_market_sell_order(SYMBOL, sell_qty)
                        exit_price = float(order.get('average') or current_price)
                        profit = (exit_price - current_pos['buy_price']) * sell_qty

                        # تحديث حالة الصفقة في قاعدة البيانات إلى CLOSED
                        total_prev_profit = get_cumulative_profit()
                        new_cumulative = total_prev_profit + profit

                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE trades 
                                    SET status = 'CLOSED', sell_price = %s, net_profit_usd = %s,
                                        cumulative_profit = %s, closed_at = CURRENT_TIMESTAMP
                                    WHERE id = %s;
                                """, (exit_price, profit, new_cumulative, current_pos['id']))
                            conn.commit()

                        send_telegram(
                            f"💰 *تم إغلاق الصفقة (#{current_pos['id']}) بنجاح!*\n"
                            f"• سعر البيع: `{exit_price:.2f}$`\n"
                            f"• ربح الصفقة: `+{profit:.2f} USDT`\n"
                            f"• إجمالي الأرباح التراكمية: `+{new_cumulative:.2f} USDT`"
                        )
                        current_pos = None

            time.sleep(10)

        except Exception as e:
            print(f"خطأ في دورة التداول: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_bot()
