import os
import time
import requests
import psycopg2
import ccxt

# ==================== الإعدادات والمتغيرات البيئية ====================
API_KEY = os.getenv("MEXC_API_KEY")
API_SECRET = os.getenv("MEXC_API_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_URL = os.getenv("DATABASE_URL")

# تهيئة المنصة (تداول فوري بدون رافعة)
exchange = ccxt.mexc({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

SYMBOL = 'SOL/USDT'
BASE_COIN = 'SOL'
QUOTE_COIN = 'USDT'

MAX_OPEN_POSITIONS = 4        # أقصى عدد صفقات مفتوحة في نفس الوقت
POSITION_PERCENTAGE = 0.25    # دخول بـ 25% من إجمالي المحفظة لكل صفقة
TAKE_PROFIT_RATIO = 0.01      # هدف جني ربح 1.0%

# ==================== إدارة قاعدة البيانات ====================
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
                    cost_usd NUMERIC(14, 4) NOT NULL,
                    net_profit_usd NUMERIC(14, 4) DEFAULT 0.0,
                    cumulative_profit NUMERIC(14, 4) DEFAULT 0.0,
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
            """)
        conn.commit()

def get_open_positions():
    """استرجاع جميع الصفقات المفتوحة حالياً لمتابعتها"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, buy_price, quantity, cost_usd 
                FROM trades 
                WHERE status = 'OPEN' AND symbol = %s 
                ORDER BY id ASC;
            """, (SYMBOL,))
            rows = cur.fetchall()
            return [{"id": r[0], "buy_price": float(r[1]), "qty": float(r[2]), "cost": float(r[3])} for r in rows]

def get_cumulative_profit():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(net_profit_usd), 0.0) FROM trades WHERE status = 'CLOSED';")
            return float(cur.fetchone()[0])

# ==================== نظام الإشعارات ====================
def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"خطأ تيليجرام: {err}")

# ==================== دورة التداول الأساسية ====================
def run_bot():
    init_db()
    send_telegram(
        f"🚀 *تم تشغيل البوت بنجاح على منصة MEXC*\n"
        f"• النظام: `Spot Scalping 1:1`\n"
        f"• الزوج: `{SYMBOL}`\n"
        f"• توزيع رأس المال: `25% لكل صفقة (حد أقصى 4 صفقات)`\n"
        f"• الهدف: `1.0% لكل صفقة`"
    )

    while True:
        try:
            ticker = exchange.fetch_ticker(SYMBOL)
            current_price = float(ticker['last'])
            
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get(QUOTE_COIN, 0.0))
            total_equity = float(balance['total'].get(QUOTE_COIN, 0.0)) + (float(balance['total'].get(BASE_COIN, 0.0)) * current_price)

            open_positions = get_open_positions()
            current_open_count = len(open_positions)

            # 1. فحص إمكانية فتح صفقة جديدة (أقل من 4 مراكز وتوفر كاش كافٍ)
            trade_size_usd = total_equity * POSITION_PERCENTAGE
            if current_open_count < MAX_OPEN_POSITIONS and usdt_free >= trade_size_usd and trade_size_usd >= 10.0:
                # التأكد من عدم الشراء عند نفس سعر آخر صفقة مباشرة (شرط فارق 0.5% على الأقل لتفادي التكرار اللحظي)
                can_buy = True
                if open_positions:
                    last_buy = open_positions[-1]['buy_price']
                    if abs(current_price - last_buy) / last_buy < 0.005:
                        can_buy = False

                if can_buy:
                    amount_to_buy = round(trade_size_usd / current_price, 2)
                    order = exchange.create_market_buy_order(SYMBOL, amount_to_buy)
                    entry_price = float(order.get('average') or current_price)
                    actual_cost = entry_price * amount_to_buy

                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO trades (symbol, status, buy_price, quantity, cost_usd)
                                VALUES (%s, 'OPEN', %s, %s, %s) RETURNING id;
                            """, (SYMBOL, entry_price, amount_to_buy, actual_cost))
                            trade_id = cur.fetchone()[0]
                        conn.commit()

                    target_tp = entry_price * (1 + TAKE_PROFIT_RATIO)
                    send_telegram(
                        f"🟢 *صفقة جديدة (#{trade_id})*\n"
                        f"• المركز: `{current_open_count + 1}/{MAX_OPEN_POSITIONS}`\n"
                        f"• القيمة: `{actual_cost:.2f}$ (25%)`\n"
                        f"• سعر الدخول: `{entry_price:.2f}$`\n"
                        f"• هدف البيع: `{target_tp:.2f}$`"
                    )
                    open_positions = get_open_positions()

            # 2. فحص أهداف جني الأرباح لجميع الصفقات المفتوحة
            for pos in open_positions:
                target_sell_price = pos['buy_price'] * (1 + TAKE_PROFIT_RATIO)
                if current_price >= target_sell_price:
                    sol_free = float(exchange.fetch_balance()['free'].get(BASE_COIN, 0.0))
                    sell_qty = min(pos['qty'], sol_free)

                    if sell_qty > 0:
                        order = exchange.create_market_sell_order(SYMBOL, sell_qty)
                        exit_price = float(order.get('average') or current_price)
                        net_profit = (exit_price - pos['buy_price']) * sell_qty

                        prev_profit = get_cumulative_profit()
                        new_cumulative = prev_profit + net_profit

                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE trades 
                                    SET status = 'CLOSED', sell_price = %s, net_profit_usd = %s,
                                        cumulative_profit = %s, closed_at = CURRENT_TIMESTAMP
                                    WHERE id = %s;
                                """, (exit_price, net_profit, new_cumulative, pos['id']))
                            conn.commit()

                        send_telegram(
                            f"💰 *تم إغلاق الصفقة (#{pos['id']}) بنجاح!*\n"
                            f"• سعر البيع: `{exit_price:.2f}$`\n"
                            f"• صافي الربح: `+{net_profit:.2f} USDT`\n"
                            f"• إجمالي الأرباح التراكمية: `+{new_cumulative:.2f} USDT`\n"
                            f"• الرصيد الإجمالي التقديري: `{total_equity + net_profit:.2f}$`"
                        )

            time.sleep(10)

        except Exception as e:
            print(f"خطأ أثناء تشغيل الدورة: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_bot()
