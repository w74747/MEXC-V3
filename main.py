import os
import time
import math
import requests
import psycopg2
import ccxt
import numpy as np
from datetime import datetime

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

MAX_SLOTS = 8                 # سعة 8 مراكز متزامنة
FIXED_TRADE_USD = 100.0       # 100$ لكل مركز
TP_PERCENT = 0.0035           # هدف ربح +0.35% (أمر صانع Maker)
TAKER_FEE_RATE = 0.001        # عمولة الشراء (0.1% Taker)
MAX_HOLD_SECONDS = 3600       # إغلاق زمني إذا تجاوزت الصفقة 60 دقيقة
TIME_SL_PERCENT = 0.012       # وقف خسارة طارئ -1.2% للمراكز العالقة
BOLLINGER_STD = 1.5           # حساسية البولنجر اللحظية (1m)

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
                cur.execute("SELECT COALESCE(SUM(net_profit_usd), 0.0) FROM trades WHERE status = 'CLOSED';")
                return float(cur.fetchone()[0])
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

# ==================== مؤشر البولنجر ====================
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
        print(f"خطأ مؤشر {symbol}: {err}")
        return None, None, None

# ==================== استعلام الصفقات النشطة ====================
def fetch_active_trades():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, sell_order_id, buy_price, quantity, cost_usd, opened_at 
                FROM trades 
                WHERE status = 'OPEN' 
                ORDER BY id ASC;
            """)
            rows = cur.fetchall()
            return [{
                "id": r[0], "symbol": r[1], "sell_order_id": r[2],
                "buy_price": float(r[3]), "qty": float(r[4]), 
                "cost": float(r[5]), "opened_at": r[6]
            } for r in rows]

# ==================== احتساب القيمة الحقيقية للمحفظة ====================
def calculate_portfolio_equity(usdt_free: float, open_trades: list):
    unrealized_pnl = 0.0
    active_assets_val = 0.0

    for t in open_trades:
        try:
            ticker = exchange.fetch_ticker(t['symbol'])
            current_price = float(ticker['last'])
            current_val = t['qty'] * current_price
            active_assets_val += current_val
            unrealized_pnl += (current_val - t['cost'])
        except Exception:
            active_assets_val += t['cost']

    total_net_worth = usdt_free + active_assets_val
    return total_net_worth, unrealized_pnl

# ==================== محرك التداول الأساسي ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("خطأ: مفاتيح المنصة غير متوفرة.")
        return

    init_db()

    send_telegram(
        f"🛡️ *تم تفعيل درع الشفافية والخروج الزمني*\n"
        f"• رصد القيمة الحقيقية (MTM) والخسائر العائمة لحظياً.\n"
        f"• الخروج الزمني التلقائي: `تسييل المراكز بعد 60 دقيقة إذا كسرت -1.2%`.\n"
        f"• الهدف: `حماية رأس المال ومنع تجميد السيولة في مسارات هابطة`."
    )

    last_equity_report = 0

    while True:
        try:
            bal = exchange.fetch_balance({'type': 'spot'})
            usdt_free = float(bal['free'].get('USDT', 0.0))
            open_trades = fetch_active_trades()

            # 1. مراقبة الأوامر وإدارة الخروج الزمني للمراكز العالقة
            for t in open_trades:
                sym = t['symbol']
                sell_id = t['sell_order_id']
                t_id = t['id']
                opened_at = t['opened_at']

                # أ. فحص أمر البيع الهدف Limit
                target_hit = False
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

                            total_acc = get_cumulative_profit()
                            send_telegram(
                                f"🎯 *جني ربح لحظي (#{t_id})*\n"
                                f"• الزوج: `{sym}`\n"
                                f"• سعر البيع: `{sold_price:.8g}$`\n"
                                f"• الربح الصافي: `+{net_profit:.2f} USDT`\n"
                                f"• إجمالي الأرباح المحققة: `+{total_acc:.2f} USDT`"
                            )
                            target_hit = True
                    except Exception:
                        pass

                if target_hit:
                    continue

                # ب. تطبيق الخروج الزمني (Time-Based Exit)
                hold_duration = (datetime.now() - opened_at).total_seconds()
                if hold_duration > MAX_HOLD_SECONDS:
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        cur_price = float(ticker['last'])
                        loss_pct = (cur_price - t['buy_price']) / t['buy_price']

                        if loss_pct <= -TIME_SL_PERCENT:
                            # إلغاء أمر البيع المعلق
                            if sell_id:
                                try:
                                    exchange.cancel_order(sell_id, sym)
                                except Exception:
                                    pass
                                time.sleep(0.3)

                            # بيع المركز بسعر السوق لتحرير الكاش
                            sell_qty = apply_step_size(sym, t['qty'])
                            exchange.create_market_sell_order(sym, sell_qty)
                            actual_loss = (cur_price - t['buy_price']) * sell_qty - (t['cost'] * TAKER_FEE_RATE)

                            with get_db_connection() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE trades 
                                        SET status = 'CLOSED', sell_price = %s, net_profit_usd = %s, closed_at = CURRENT_TIMESTAMP
                                        WHERE id = %s;
                                    """, (cur_price, actual_loss, t_id))
                                conn.commit()

                            send_telegram(
                                f"⏱️ *إغلاق زمني وقائي للمركز (#{t_id})*\n"
                                f"• الزوج: `{sym}` | مدة الاحتجاز: `{hold_duration/60:.0f} دقيقة`\n"
                                f"• الخسارة المنفذة: `{actual_loss:.2f} USDT` ({loss_pct*100:.2f}%)\n"
                                f"• تم تحرير السيولة لإعادة التدوير."
                            )
                    except Exception as err:
                        print(f"خطأ الخروج الزمني: {err}")

            # 2. تقرير القيمة الحقيقية للمحفظة دورياً (كل ساعتين)
            now_ts = time.time()
            if now_ts - last_equity_report > 7200:
                net_worth, unrealized = calculate_portfolio_equity(usdt_free, open_trades)
                send_telegram(
                    f"📊 *تقرير السيولة والقيمة الحقيقية (MTM)*\n"
                    f"• رصيد الكاش الحر: `{usdt_free:.2f}$ USDT`\n"
                    f"• القيمة الإجمالية الفعلية: `{net_worth:.2f}$`\n"
                    f"• الفارق السعري العائم: `{unrealized:+.2f}$ USDT`\n"
                    f"• المراكز المفتوحة: `{len(open_trades)}/{MAX_SLOTS}`"
                )
                last_equity_report = now_ts

            # 3. فحص فرص الشراء الجديدة بحجم 100$
            open_trades = fetch_active_trades()
            active_symbols = set(t['symbol'] for t in open_trades)

            if len(open_trades) < MAX_SLOTS and usdt_free >= FIXED_TRADE_USD:
                for symbol in WATCHLIST:
                    base = symbol.split('/')[0]

                    if symbol in active_symbols:
                        continue

                    # فحص عدم وجود رصيد متبقٍ غير مسجل
                    base_balance = float(bal['total'].get(base, 0.0))
                    ticker = exchange.fetch_ticker(symbol)
                    cur_price = float(ticker['last'])
                    if (base_balance * cur_price) > 2.0:
                        continue

                    lower_band, sma, cur_close = get_bollinger_bands(symbol)
                    time.sleep(0.08)

                    if not lower_band:
                        continue

                    # شرط الدخول: شمعة الدقيقة كسرت الحد السفلي
                    if cur_close <= lower_band:
                        amount_to_buy = apply_step_size(symbol, FIXED_TRADE_USD / cur_close)
                        active_symbols.add(symbol)

                        # تنفيذ الشراء
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
                            f"🟢 *صفقة جديدة (#{t_new_id})*\n"
                            f"• الزوج: `{symbol}`\n"
                            f"• سعر الدخول: `{entry_price:.8g}$`\n"
                            f"• أمر البيع المعلق (Limit): `{sell_target_price:.8g}$` (+0.35%)\n"
                            f"• الحجم: `{actual_cost:.2f}$ (ثابتة)`\n"
                            f"• الحماية: `تسييل زمني تلقائي بعد 60 دقيقة`"
                        )
                        break

            time.sleep(4)

        except Exception as loop_err:
            print(f"خطأ دورة المحرك: {loop_err}")
            time.sleep(6)

if __name__ == "__main__":
    run_bot()
