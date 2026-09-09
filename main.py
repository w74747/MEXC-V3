import os
import time
import requests
import psycopg2
import ccxt

# ==================== الإعدادات والمتغيرات ====================
API_KEY = os.getenv("MEXC_API_KEY", "").strip()
API_SECRET = os.getenv("MEXC_API_SECRET", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DB_URL = os.getenv("DATABASE_URL", "").strip()

# سلة الأزواج الأكثر نشاطاً وسيولة على MEXC
WATCHLIST = [
    'SOL/USDT', 'ETH/USDT', 'DOGE/USDT', 
    'NEAR/USDT', 'AVAX/USDT', 'SUI/USDT', 'LINK/USDT'
]

MAX_OPEN_POSITIONS = 4        # أقصى عدد صفقات مفتوحة معاً (مركز واحد لكل عملة)
POSITION_PERCENTAGE = 0.25    # 25% من إجمالي المحفظة لكل صفقة
TAKE_PROFIT_RATIO = 0.01      # هدف جني ربح 1.0%

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
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, buy_price, quantity, cost_usd 
                FROM trades 
                WHERE status = 'OPEN' 
                ORDER BY id ASC;
            """)
            rows = cur.fetchall()
            return [{"id": r[0], "symbol": r[1], "buy_price": float(r[2]), "qty": float(r[3]), "cost": float(r[4])} for r in rows]

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

# ==================== التصفية المبدئية عند التشغيل ====================
def emergency_flush():
    """بيع أي صفقات أو عملات معلقة لبدء الحساب بكاش USDT نقي 100%"""
    try:
        balance = exchange.fetch_balance()
        sol_free = float(balance['free'].get('SOL', 0.0))
        
        # بيع أي كمية SOL متبقية من الصفقات السابقة
        if sol_free > 0.05:
            ticker = exchange.fetch_ticker('SOL/USDT')
            cur_price = float(ticker['last'])
            order = exchange.create_market_sell_order('SOL/USDT', sol_free)
            sold_price = float(order.get('average') or cur_price)
            print(f"تمت تصفية {sol_free} SOL بسعر {sold_price}$ لبدء التشغيل النظيف.")
            
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE trades 
                    SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP 
                    WHERE status = 'OPEN';
                """)
            conn.commit()
            
        send_telegram("🧹 *تم تنظيف المحفظة وإغلاق المراكز السابقة بنجاح*\nالبوت جاهز للبدء بسيولة USDT نقدية كاملة.")
    except Exception as err:
        print(f"تنبيه أثناء التصفية المبدئية: {err}")

# ==================== محرك التداول اللحظي ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("خطأ فادح: لم يتم العثور على مفاتيح المنصة في البيئة.")
        return

    init_db()
    emergency_flush()

    send_telegram(
        f"🚀 *تم إطلاق ماسح التداول متعدد الأزواج (Spot 1:1)*\n"
        f"• قائمة المراقبة: `SOL, ETH, DOGE, NEAR, AVAX, SUI, LINK`\n"
        f"• عدد المراكز: `4 صفقات كحد أقصى (عملة مختلفة لكل مركز)`\n"
        f"• الحصة: `25% لكل صفقة ديناميكياً`\n"
        f"• هدف جني الربح: `+1.0% لكل صفقة`"
    )

    while True:
        try:
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get('USDT', 0.0))
            total_equity = float(balance['total'].get('USDT', 0.0))

            # حساب القيمة الإجمالية للمحفظة مع الأصول المفتوحة
            open_positions = get_open_positions()
            current_open_count = len(open_positions)
            active_symbols = [pos['symbol'] for pos in open_positions]

            # 1. متابعة وفحص أهداف جني الأرباح (1.0%) للصفقات المفتوحة
            for pos in open_positions:
                sym = pos['symbol']
                base_coin = sym.split('/')[0]
                ticker = exchange.fetch_ticker(sym)
                cur_price = float(ticker['last'])

                target_exit = pos['buy_price'] * (1 + TAKE_PROFIT_RATIO)
                if cur_price >= target_exit:
                    coin_free = float(exchange.fetch_balance()['free'].get(base_coin, 0.0))
                    sell_qty = min(pos['qty'], coin_free)

                    if sell_qty > 0:
                        order = exchange.create_market_sell_order(sym, sell_qty)
                        exit_price = float(order.get('average') or cur_price)
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
                            f"💰 *تم جني الربح بنجاح! (#{pos['id']})*\n"
                            f"• العملة: `{sym}`\n"
                            f"• سعر الشراء: `{pos['buy_price']:.4f}$`\n"
                            f"• سعر البيع: `{exit_price:.4f}$`\n"
                            f"• صافي الربح: `+{net_profit:.2f} USDT`\n"
                            f"• إجمالي الأرباح التراكمية: `+{new_cumulative:.2f} USDT`"
                        )

            # تحديث قائمة الصفقات المفتوحة بعد البيع
            open_positions = get_open_positions()
            current_open_count = len(open_positions)
            active_symbols = [pos['symbol'] for pos in open_positions]

            # 2. البحث عن فرص جديدة للأزواج الشاغرة
            trade_size_usd = total_equity * POSITION_PERCENTAGE
            if current_open_count < MAX_OPEN_POSITIONS and usdt_free >= trade_size_usd and trade_size_usd >= 10.0:
                for target_symbol in WATCHLIST:
                    # شرط عدم تكرار نفس العملة في أكثر من مركز
                    if target_symbol in active_symbols:
                        continue

                    # فحص شروط الدخول السريع
                    ticker = exchange.fetch_ticker(target_symbol)
                    current_price = float(ticker['last'])
                    base_coin = target_symbol.split('/')[0]

                    # حساب الكمية وتفادي أخطاء التقريب
                    amount_to_buy = trade_size_usd / current_price
                    amount_to_buy = float(exchange.amount_to_precision(target_symbol, amount_to_buy))

                    order = exchange.create_market_buy_order(target_symbol, amount_to_buy)
                    entry_price = float(order.get('average') or current_price)
                    actual_cost = entry_price * amount_to_buy

                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO trades (symbol, status, buy_price, quantity, cost_usd)
                                VALUES (%s, 'OPEN', %s, %s, %s) RETURNING id;
                            """, (target_symbol, entry_price, amount_to_buy, actual_cost))
                            trade_id = cur.fetchone()[0]
                        conn.commit()

                    target_tp = entry_price * (1 + TAKE_PROFIT_RATIO)
                    send_telegram(
                        f"🟢 *صفقة شراء جديدة (#{trade_id})*\n"
                        f"• العملة: `{target_symbol}`\n"
                        f"• المركز: `{len(active_symbols) + 1}/{MAX_OPEN_POSITIONS}`\n"
                        f"• الكمية: `{amount_to_buy} {base_coin}`\n"
                        f"• القيمة: `{actual_cost:.2f}$ (25%)`\n"
                        f"• سعر الدخول: `{entry_price:.4f}$`\n"
                        f"• هدف البيع (+1.0%): `{target_tp:.4f}$`"
                    )

                    active_symbols.append(target_symbol)
                    usdt_free -= actual_cost
                    if len(active_symbols) >= MAX_OPEN_POSITIONS or usdt_free < trade_size_usd:
                        break

            time.sleep(10)

        except Exception as e:
            print(f"خطأ أثناء تشغيل الدورة: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run_bot()
