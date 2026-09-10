import os
import time
import math
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
    'SOL/USDT', 'ETH/USDT', 'DOGE/USDT', 'NEAR/USDT', 
    'AVAX/USDT', 'SUI/USDT', 'LINK/USDT', 'XRP/USDT', 
    'BNB/USDT', 'ADA/USDT', 'APT/USDT', 'INJ/USDT', 
    'DOT/USDT', 'POL/USDT', 'PEPE/USDT', 'SHIB/USDT'
]

MAX_SLOTS = 8                 # زيادة السعة إلى 8 مراكز لتفادي تجمد السيولة
FIXED_TRADE_USD = 100.0       # 100$ ثابتة لكل مركز
TP_PERCENT = 0.0035           # هدف ربح +0.35% (أمر Limit صانع بصفر رسوم)
TAKER_FEE_RATE = 0.001        # عمولة الشراء بسعر السوق (0.1% Taker)
BOLLINGER_STD = 1.5           # حساسية البولنجر اللحظية (فريم 1 دقيقة)

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
                    sell_order_id VARCHAR(64),
                    buy_price NUMERIC(18, 8) NOT NULL,
                    sell_price NUMERIC(18, 8),
                    quantity NUMERIC(18, 8) NOT NULL,
                    cost_usd NUMERIC(14, 4) NOT NULL,
                    net_profit_usd NUMERIC(14, 4) DEFAULT 0.0,
                    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP
                );
            """)
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS sell_order_id VARCHAR(64);")
        conn.commit()

def get_cumulative_profit() -> float:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COALESCE(SUM(net_profit_usd), 0.0) 
                    FROM trades 
                    WHERE status = 'CLOSED';
                """)
                total = cur.fetchone()[0]
                return float(total)
    except Exception:
        return 0.0

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

# ==================== إدارة دقة الأحجام ====================
def apply_step_size(symbol: str, qty: float) -> float:
    try:
        mkt = exchange.market(symbol)
        precision = mkt.get("precision", {}).get("amount", 4)
        if isinstance(precision, int):
            factor = 10 ** precision
            return math.floor(qty * factor) / factor
        elif isinstance(precision, float) and precision > 0:
            return math.floor(qty / precision) * precision
    except Exception:
        pass
    return round(qty, 4)

# ==================== مؤشر البولنجر اللحظي ====================
def get_bollinger_bands(symbol: str, period: int = 20, num_std: float = BOLLINGER_STD):
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
        print(f"خطأ بيانات {symbol}: {err}")
        return None, None, None

# ==================== استعلام الصفقات النشطة ====================
def fetch_active_trades():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, sell_order_id, buy_price, quantity, cost_usd 
                FROM trades 
                WHERE status = 'OPEN' 
                ORDER BY id ASC;
            """)
            rows = cur.fetchall()
            return [{
                "id": r[0], "symbol": r[1], "sell_order_id": r[2],
                "buy_price": float(r[3]), "qty": float(r[4]), "cost": float(r[5])
            } for r in rows]

# ==================== محرك التداول الأساسي ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("خطأ: مفاتيح المنصة مفقودة.")
        return

    init_db()

    send_telegram(
        f"⚡ *تم تحديث إعدادات التداول والسيولة*\n"
        f"• قيمة المركز الجديد: `100.0$ ثابتة`\n"
        f"• السعة القصوى: `8 مراكز متزامنة`\n"
        f"• شرط الخروج: `+0.35% Maker Limit`\n"
        f"• استئناف المسح اللحظي بالسيولة المتاحة فوراً."
    )

    while True:
        try:
            bal = exchange.fetch_balance({'type': 'spot'})
            usdt_free = float(bal['free'].get('USDT', 0.0))

            open_trades = fetch_active_trades()

            # 1. متابعة أوامر البيع Limit في دفتر الأوامر
            for t in open_trades:
                sym = t['symbol']
                sell_id = t['sell_order_id']
                t_id = t['id']

                if sell_id:
                    try:
                        order = exchange.fetch_order(sell_id, sym)
                        if order['status'] == 'closed':
                            sold_price = float(order.get('average') or (t['buy_price'] * (1 + TP_PERCENT)))
                            
                            gross_profit = t['cost'] * TP_PERCENT
                            entry_fee = t['cost'] * TAKER_FEE_RATE
                            net_profit = gross_profit - entry_fee

                            with get_db_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE trades 
                                        SET status = 'CLOSED', sell_price = %s, net_profit_usd = %s, closed_at = CURRENT_TIMESTAMP
                                        WHERE id = %s;
                                    """, (sold_price, net_profit, t_id))
                                conn.commit()

                            total_accumulated = get_cumulative_profit()

                            send_telegram(
                                f"🎯 *تم جني الربح بنجاح! (#{t_id})*\n"
                                f"• العملة: `{sym}`\n"
                                f"• سعر البيع: `{sold_price:.8g}$`\n"
                                f"• الربح الصافي: `+{net_profit:.2f} USDT`\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"💰 *إجمالي الأرباح التراكمية:* `+{total_accumulated:.2f} USDT`"
                            )
                    except Exception:
                        pass

            # 2. فحص سلة الـ 16 زوجاً للشراء بحجم 100$
            open_trades = fetch_active_trades()
            active_symbols = set(t['symbol'] for t in open_trades)

            if len(open_trades) < MAX_SLOTS and usdt_free >= FIXED_TRADE_USD:
                for symbol in WATCHLIST:
                    base = symbol.split('/')[0]

                    if symbol in active_symbols:
                        continue

                    # فحص وجود رصيد معلق للعملة في المحفظة (> 2$) لمنع التكرار
                    base_balance = float(bal['total'].get(base, 0.0))
                    ticker = exchange.fetch_ticker(symbol)
                    cur_price = float(ticker['last'])
                    if (base_balance * cur_price) > 2.0:
                        continue

                    lower_band, sma, cur_close = get_bollinger_bands(symbol)
                    time.sleep(0.08)

                    if not lower_band:
                        continue

                    # كسر الحد السفلي للبولنجر
                    if cur_close <= lower_band:
                        amount_to_buy = apply_step_size(symbol, FIXED_TRADE_USD / cur_close)
                        active_symbols.add(symbol)

                        # تنفيذ الشراء المباشر بقيمة 100$
                        buy_order = exchange.create_market_buy_order(symbol, amount_to_buy)
                        entry_price = float(buy_order.get('average') or cur_close)
                        actual_cost = entry_price * amount_to_buy

                        sell_target_price = entry_price * (1 + TP_PERCENT)
                        sell_target_price = float(exchange.price_to_precision(symbol, sell_target_price))

                        time.sleep(0.3)
                        base_bal = float(exchange.fetch_balance({'type': 'spot'})['free'].get(base, 0.0))
                        actual_sell_qty = apply_step_size(symbol, min(amount_to_buy, base_bal))

                        sell_order = exchange.create_limit_sell_order(symbol, actual_sell_qty, sell_target_price)
                        sell_order_id = sell_order['id']

                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO trades (symbol, status, sell_order_id, buy_price, quantity, cost_usd)
                                    VALUES (%s, 'OPEN', %s, %s, %s, %s) RETURNING id;
                                """, (symbol, sell_order_id, entry_price, actual_sell_qty, actual_cost))
                                t_new_id = cur.fetchone()[0]
                            conn.commit()

                        send_telegram(
                            f"🟢 *صفقة سكالبينج جديدة (#{t_new_id})*\n"
                            f"• الزوج: `{symbol}`\n"
                            f"• سعر الدخول: `{entry_price:.8g}$`\n"
                            f"• أمر البيع المعلق (Limit): `{sell_target_price:.8g}$` (+0.35%)\n"
                            f"• القيمة المستثمرة: `{actual_cost:.2f}$ (ثابتة)`\n"
                            f"• الرسوم: `0% Maker Fee`"
                        )
                        break

            time.sleep(4)

        except Exception as loop_err:
            print(f"خطأ الدورة: {loop_err}")
            time.sleep(6)

if __name__ == "__main__":
    run_bot()
