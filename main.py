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

# سلة متوازنة وعالية السيولة (16 زوجاً نشطاً)
WATCHLIST = [
    'SOL/USDT', 'ETH/USDT', 'DOGE/USDT', 'NEAR/USDT', 
    'AVAX/USDT', 'SUI/USDT', 'LINK/USDT', 'XRP/USDT', 
    'BNB/USDT', 'ADA/USDT', 'APT/USDT', 'INJ/USDT', 
    'DOT/USDT', 'POL/USDT', 'PEPE/USDT', 'SHIB/USDT'
]

MAX_SLOTS = 4                 # 4 أزواج متزامنة كحد أقصى (توزيع مخاطر 1:1)
SLOT_PERCENTAGE = 0.25        # 25% من إجمالي المحفظة لكل صفقة
TP_PERCENT = 0.0035           # هدف ربح لحظي +0.35% (أمر Limit صانع بصفر رسوم)
BOLLINGER_STD = 1.5           # حساسية البولنجر السريعة (فريم 1 دقيقة)

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

def get_open_trades():
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

# ==================== حساب مؤشر Bollinger Bands ====================
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

# ==================== تصفية البداية ====================
def clean_start_flush():
    try:
        balance = exchange.fetch_balance()
        for symbol in WATCHLIST:
            base = symbol.split('/')[0]
            qty = float(balance['free'].get(base, 0.0))
            if qty > 0:
                ticker = exchange.fetch_ticker(symbol)
                cur_price = float(ticker['last'])
                if (qty * cur_price) > 5.0:
                    exchange.create_market_sell_order(symbol, qty)
                    print(f"تمت تصفية {qty} {base}")

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE trades SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP WHERE status = 'OPEN';")
            conn.commit()

        send_telegram("🧹 *تم إفراغ المراكز السابقة وتجهيز الرصيد كاش 100% USDT.*\nبدء مسح سلة الـ 16 زوجاً.")
    except Exception as err:
        print(f"تنبيه أثناء التصفية المبدئية: {err}")

# ==================== محرك التداول ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("خطأ: المفاتيح غير معرفة.")
        return

    init_db()
    clean_start_flush()

    send_telegram(
        f"⚡ *تم تفعيل ماسح الـ 16 زوجاً فائق السرعة*\n"
        f"• العملات المراقبة: `16 عملة قيادية وعالية السيولة`\n"
        f"• الحد الأقصى للمراكز: `4 صفقات متزامنة (25% لكل مركز)`\n"
        f"• الحساسية: `Std 1.5 على فريم 1 دقيقة`\n"
        f"• نوع أمر الخروج: `Limit TP (+0.35% بصفر رسوم صانع)`"
    )

    while True:
        try:
            balance = exchange.fetch_balance()
            usdt_free = float(balance['free'].get('USDT', 0.0))
            total_equity = float(balance['total'].get('USDT', 0.0))

            open_trades = get_open_trades()
            trade_size_usd = total_equity * SLOT_PERCENTAGE

            # 1. متابعة أوامر البيع الـ Limit المعلقة في دفتر الأوامر
            for t in open_trades:
                sym = t['symbol']
                sell_id = t['sell_order_id']
                t_id = t['id']

                if sell_id:
                    try:
                        order_info = exchange.fetch_order(sell_id, sym)
                        if order_info['status'] == 'closed':
                            sold_price = float(order_info.get('average') or (t['buy_price'] * (1 + TP_PERCENT)))
                            profit = (sold_price - t['buy_price']) * t['qty']

                            with get_db_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE trades 
                                        SET status = 'CLOSED', sell_price = %s, net_profit_usd = %s, closed_at = CURRENT_TIMESTAMP
                                        WHERE id = %s;
                                    """, (sold_price, profit, t_id))
                                conn.commit()

                            send_telegram(
                                f"🎯 *تم تنفيذ هدف الربح بنجاح! (#{t_id})*\n"
                                f"• الزوج: `{sym}`\n"
                                f"• سعر البيع: `{sold_price:.4f}$`\n"
                                f"• صافي الربح: `+{profit:.2f} USDT` (0% Maker Fee)"
                            )
                    except Exception:
                        pass

            # 2. فحص الـ 16 زوجاً لاقتناص أي قاع لحظي
            open_trades = get_open_trades()
            active_symbols = [t['symbol'] for t in open_trades]

            if len(open_trades) < MAX_SLOTS and usdt_free >= trade_size_usd and trade_size_usd >= 15.0:
                for symbol in WATCHLIST:
                    if symbol in active_symbols:
                        continue

                    lower_band, sma, cur_close = get_bollinger_bands(symbol)
                    time.sleep(0.08)  # حماية معدل الطلبات (Rate Limit Protection)

                    if not lower_band:
                        continue

                    # شرط الدخول: السعر لامس أو كسر الحد السفلي للبولنجر
                    if cur_close <= lower_band:
                        base = symbol.split('/')[0]
                        amount_to_buy = trade_size_usd / cur_close
                        amount_to_buy = float(exchange.amount_to_precision(symbol, amount_to_buy))

                        # تنفيذ الشراء المباشر
                        buy_order = exchange.create_market_buy_order(symbol, amount_to_buy)
                        entry_price = float(buy_order.get('average') or cur_close)
                        actual_cost = entry_price * amount_to_buy

                        # تعليق أمر جني الربح Limit في دفتر الأوامر
                        sell_target_price = entry_price * (1 + TP_PERCENT)
                        sell_target_price = float(exchange.price_to_precision(symbol, sell_target_price))

                        time.sleep(0.4)
                        base_bal = float(exchange.fetch_balance()['free'].get(base, 0.0))
                        actual_sell_qty = min(amount_to_buy, base_bal)
                        actual_sell_qty = float(exchange.amount_to_precision(symbol, actual_sell_qty))

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
                            f"• سعر الدخول: `{entry_price:.4f}$`\n"
                            f"• أمر البيع المعلق (Limit): `{sell_target_price:.4f}$` (+0.35%)\n"
                            f"• القيمة: `{actual_cost:.2f}$ (25%)`\n"
                            f"• الرسوم: `0% Maker Fee`"
                        )
                        break

            time.sleep(5)

        except Exception as loop_err:
            print(f"خطأ في الدورة: {loop_err}")
            time.sleep(7)

if __name__ == "__main__":
    run_bot()
