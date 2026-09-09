import os
import time
import requests
import psycopg2
import ccxt

# ==================== استدعاء المتغيرات بنفس الأسماء المعتمدة ====================
API_KEY = os.getenv("MEXC_API_KEY", "").strip()
API_SECRET = os.getenv("MEXC_API_SECRET", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_URL = os.getenv("DATABASE_URL", "").strip()

# ==================== تهيئة المنصة ====================
exchange = ccxt.mexc({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'spot',
        'createMarketBuyOrderRequiresPrice': False
    }
})

# تأكيد تعيين المفاتيح للكائن مباشرة
exchange.apiKey = API_KEY
exchange.secret = API_SECRET

SYMBOL = 'SOL/USDT'
BASE_COIN = 'SOL'
QUOTE_COIN = 'USDT'

MAX_OPEN_POSITIONS = 4        # أقصى عدد صفقات مفتوحة معاً
POSITION_PERCENTAGE = 0.25    # 25% من إجمالي المحفظة لكل صفقة
TAKE_PROFIT_RATIO = 0.01      # هدف ربح 1.0%

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
    """استرجاع الصفقات المفتوحة حالياً"""
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
            row = cur.fetchone()
            return float(row[0]) if row else 0.0

# ==================== تيليجرام ====================
def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as err:
        print(f"خطأ تيليجرام: {err}")

# ==================== دورة التشغيل ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("خطأ فادح: لم يتم العثور على MEXC_API_KEY أو MEXC_API_SECRET في البيئة.")
        return

    init_db()
    send_telegram(
        f"🚀 *تم تشغيل البوت بنجاح على منصة MEXC*\n"
        f"• النظام: `Spot Scalping 1:1`\n"
        f"• الزوج: `{SYMBOL}`\n"
        f"• التوزيع: `25% لكل صفقة (حد أقصى 4 صفقات)`\n"
        f"• الهدف: `1.0% لكل صفقة`"
    )

    while True:
        try:
            # 1. جلب السعر اللحظي
            ticker = exchange.fetch_ticker(SYMBOL)
            current_price = float(ticker['last'])

            # 2. جلب الرصيد
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get(QUOTE_COIN, 0.0))
            usdt_total = float(balance['total'].get(QUOTE_COIN, 0.0))
            sol_total = float(balance['total'].get(BASE_COIN, 0.0))
            total_equity = usdt_total + (sol_total * current_price)

            # 3. الصفقات المفتوحة
            open_positions = get_open_positions()
            current_open_count = len(open_positions)

            # 4. فحص شروط الشراء (أقل من 4 صفقات، وتوفر رصيد كافٍ)
            trade_size_usd = total_equity * POSITION_PERCENTAGE
            if current_open_count < MAX_OPEN_POSITIONS and usdt_free >= trade_size_usd and trade_size_usd >= 10.0:
                can_buy = True
                if open_positions:
                    last_buy = open_positions[-1]['buy_price']
                    # منع التكرار اللحظي عند نفس السعر (اشتراط فارق 0.4% على الأقل)
                    if abs(current_price - last_buy) / last_buy < 0.004:
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
                        f"🟢 *صفقة شراء جديدة (#{trade_id})*\n"
                        f"• المركز: `{current_open_count + 1}/{MAX_OPEN_POSITIONS}`\n"
                        f"• القيمة: `{actual_cost:.2f}$ (25%)`\n"
                        f"• سعر الدخول: `{entry_price:.2f}$`\n"
                        f"• هدف البيع (1%): `{target_tp:.2f}$`"
                    )
                    open_positions = get_open_positions()

            # 5. مراقبة جني الأرباح لكل صفقة مفتوحة
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
                            f"💰 *تم جني الربح بنجاح للصفقة (#{pos['id']})!*\n"
                            f"• سعر البيع: `{exit_price:.2f}$`\n"
                            f"• صافي الربح: `+{net_profit:.2f} USDT`\n"
                            f"• إجمالي الأرباح التراكمية: `+{new_cumulative:.2f} USDT`\n"
                            f"• إجمالي قيمة المحفظة: `{(total_equity + net_profit):.2f}$`"
                        )

            time.sleep(10)

        except Exception as e:
            print(f"خطأ أثناء تشغيل الدورة: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_bot()
