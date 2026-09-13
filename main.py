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

# متغيرات التداول الديناميكية (قابلة للتعديل من Railway)
MAX_SLOTS = int(os.getenv("MAX_SLOTS", "4"))
FIXED_TRADE_USD = float(os.getenv("FIXED_TRADE_USD", "100.0"))
MAX_TOTAL_CAPITAL = float(os.getenv("MAX_TOTAL_CAPITAL", "400.0"))

# ثوابت الاستراتيجية
TIMEFRAME = '1h'
RSI_THRESHOLD = float(os.getenv("RSI_THRESHOLD", "38.0"))       # شرط ذروة البيع على 1h
TAKER_FEE_RATE = 0.001                                         # عمولة الدخول (0.1%)
BOLLINGER_STD = 2.0                                            # انحراف معياري لقيعان السوينغ

WATCHLIST = [
    'SOL/USDT', 'ETH/USDT', 'DOGE/USDT', 'NEAR/USDT', 
    'AVAX/USDT', 'SUI/USDT', 'LINK/USDT', 'XRP/USDT', 
    'BNB/USDT', 'ADA/USDT', 'APT/USDT', 'INJ/USDT', 
    'DOT/USDT', 'POL/USDT', 'PEPE/USDT', 'SHIB/USDT'
]

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
            # إضافة أعمدة الدعم الديناميكي والخروج الجزئي
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS sell_order_id VARCHAR(64);")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS stage INT DEFAULT 0;")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS atr NUMERIC(18, 8) DEFAULT 0;")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS remaining_qty NUMERIC(18, 8);")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS highest_price NUMERIC(18, 8);")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC(18, 8);")
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

# ==================== المؤشرات الحسابية ====================
def calculate_rsi(closes, period: int = 14) -> float:
    deltas = np.diff(closes)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0:
        return 100.0
    rs = up / down
    rsi = np.zeros_like(closes)
    rsi[:period] = 100.0 - 100.0 / (1.0 + rs)

    up_val = up
    down_val = down
    for i in range(period, len(deltas)):
        delta = deltas[i]
        if delta > 0:
            up_val = (up_val * (period - 1) + delta) / period
            down_val = (down_val * (period - 1)) / period
        else:
            up_val = (up_val * (period - 1)) / period
            down_val = (down_val * (period - 1) - delta) / period
        if down_val == 0:
            rsi[i + 1] = 100.0
        else:
            rs = up_val / down_val
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return float(rsi[-1])

def calculate_atr(ohlcv, period: int = 14) -> float:
    highs = np.array([c[2] for c in ohlcv], dtype=float)
    lows = np.array([c[3] for c in ohlcv], dtype=float)
    closes = np.array([c[4] for c in ohlcv], dtype=float)
    
    tr_list = []
    for i in range(1, len(ohlcv)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)
    return float(np.mean(tr_list[-period:]))

def get_swing_indicators(symbol: str):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=35)
        if len(ohlcv) < 25:
            return None
        closes = np.array([c[4] for c in ohlcv], dtype=float)
        
        # Bollinger Bands (1h)
        sma = float(np.mean(closes[-20:]))
        std = float(np.std(closes[-20:]))
        lower_band = sma - (BOLLINGER_STD * std)
        
        # RSI (14)
        rsi = calculate_rsi(closes, 14)
        
        # ATR (14)
        atr = calculate_atr(ohlcv, 14)
        
        cur_close = float(closes[-1])
        return {
            "lower_band": lower_band,
            "sma": sma,
            "rsi": rsi,
            "atr": atr,
            "cur_close": cur_close
        }
    except Exception as err:
        print(f"خطأ بيانات المؤشرات {symbol}: {err}")
        return None

def is_btc_bullish_swing() -> bool:
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=35)
        if len(ohlcv) < 20:
            return True
        closes = [c[4] for c in ohlcv]
        cur_btc = closes[-1]
        
        weights = np.exp(np.linspace(-1., 0., 20))
        weights /= weights.sum()
        ema20 = float(np.convolve(closes[-20:], weights, mode='valid')[0])
        return cur_btc >= ema20
    except Exception:
        return True

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

def fetch_active_trades():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, buy_price, quantity, cost_usd, stage, atr, 
                       remaining_qty, highest_price, stop_loss_price, opened_at
                FROM trades 
                WHERE status = 'OPEN' 
                ORDER BY id ASC;
            """)
            rows = cur.fetchall()
            return [{
                "id": r[0], "symbol": r[1], "buy_price": float(r[2]),
                "qty": float(r[3]), "cost": float(r[4]), "stage": int(r[5] or 0),
                "atr": float(r[6] or 0), "rem_qty": float(r[7] or r[3]),
                "highest": float(r[8] or r[2]), "sl_price": float(r[9] or 0),
                "opened_at": r[10]
            } for r in rows]

# ==================== المحرك الأساسي ====================
def run_bot():
    if not API_KEY or not API_SECRET:
        print("مفاتيح المنصة مفقودة.")
        return

    init_db()

    send_telegram(
        f"🚀 *تم إطلاق محرك السوينغ الذكي (1h Multi-Stage Engine)*\n"
        f"• الإطار الزمني: `شمعة الساعة (1h)` لمنع تآكل العمولات.\n"
        f"• فلتر الدخول: `قاع البولنجر + تشبع بيعي RSI ≤ {RSI_THRESHOLD}`.\n"
        f"• ديناميكية الأهداف (ATR Scaling):\n"
        f"  ├─ هدف 1: `بيع 50% عند +1.5 ATR` ونقل الوقف إلى الدخول.\n"
        f"  ├─ هدف 2: `بيع 25% عند +3.0 ATR`.\n"
        f"  └─ المتبقي: `25% Trailing Stop لاقتناص الانفجارات الكبرى`.\n"
        f"• السقف المخصص: `{MAX_TOTAL_CAPITAL}$` ({MAX_SLOTS} مراكز × {FIXED_TRADE_USD}$)"
    )

    while True:
        try:
            bal = exchange.fetch_balance({'type': 'spot'})
            usdt_free = float(bal['free'].get('USDT', 0.0))
            open_trades = fetch_active_trades()

            # 1. مراقبة وإدارة الصفقات النشطة (الأهداف الجزئية والوقف المتحرك)
            for t in open_trades:
                sym = t['symbol']
                t_id = t['id']
                buy_p = t['buy_price']
                atr_val = t['atr']
                stage = t['stage']
                rem_qty = t['rem_qty']
                highest = t['highest']
                sl_p = t['sl_price']

                ticker = exchange.fetch_ticker(sym)
                cur_price = float(ticker['last'])

                # تحديث أعلى سعر مسجل للمركز
                if cur_price > highest:
                    highest = cur_price
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE trades SET highest_price = %s WHERE id = %s;", (highest, t_id))
                        conn.commit()

                # أ. فحص الهدف الأول: بيع 50% ونقل الوقف للدخول (Break-Even)
                if stage == 0 and cur_price >= (buy_p + 1.5 * atr_val):
                    sell_qty = apply_step_size(sym, t['qty'] * 0.50)
                    if sell_qty > 0:
                        exchange.create_market_sell_order(sym, sell_qty)
                        net_p = (cur_price - buy_p) * sell_qty - (sell_qty * cur_price * TAKER_FEE_RATE)
                        new_rem_qty = rem_qty - sell_qty
                        new_sl = buy_p  # نقل الوقف إلى نقطة التعادل

                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE trades 
                                    SET stage = 1, remaining_qty = %s, stop_loss_price = %s, 
                                        net_profit_usd = net_profit_usd + %s 
                                    WHERE id = %s;
                                """, (new_rem_qty, new_sl, net_p, t_id))
                            conn.commit()

                        total_acc = get_cumulative_profit()
                        send_telegram(
                            f"🎯 *تحقيق الهدف الأول بنجاح TP1 (#{t_id})*\n"
                            f"• الزوج: `{sym}`\n"
                            f"• سعر التنفيذ: `{cur_price:.8g}$` (+{((cur_price-buy_p)/buy_p)*100:.2f}%)\n"
                            f"• تم بيع: `50% من الكمية` بربح `+{net_p:.2f} USDT`\n"
                            f"• حماية رأس المال: `نقل وقف الخسارة إلى سعر الدخول ({buy_p:.8g}$)`\n"
                            f"• إجمالي الأرباح: `{total_acc:+.2f} USDT`"
                        )
                    continue

                # ب. فحص الهدف الثاني: بيع 25% إضافية
                if stage == 1 and cur_price >= (buy_p + 3.0 * atr_val):
                    sell_qty = apply_step_size(sym, t['qty'] * 0.25)
                    if sell_qty > 0:
                        exchange.create_market_sell_order(sym, sell_qty)
                        net_p = (cur_price - buy_p) * sell_qty - (sell_qty * cur_price * TAKER_FEE_RATE)
                        new_rem_qty = rem_qty - sell_qty
                        # رفع الوقف لحجز أرباح المرحلة الأولى
                        new_sl = buy_p + 1.5 * atr_val

                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    UPDATE trades 
                                    SET stage = 2, remaining_qty = %s, stop_loss_price = %s, 
                                        net_profit_usd = net_profit_usd + %s 
                                    WHERE id = %s;
                                """, (new_rem_qty, new_sl, net_p, t_id))
                            conn.commit()

                        total_acc = get_cumulative_profit()
                        send_telegram(
                            f"🚀 *تحقيق الهدف الثاني بنجاح TP2 (#{t_id})*\n"
                            f"• الزوج: `{sym}`\n"
                            f"• سعر التنفيذ: `{cur_price:.8g}$` (+{((cur_price-buy_p)/buy_p)*100:.2f}%)\n"
                            f"• تم بيع: `25% إضافية` بربح `+{net_p:.2f} USDT`\n"
                            f"• الكمية المتبقية (25% Runner): `قيد الملاحقة بـ Trailing Stop`\n"
                            f"• إجمالي الأرباح: `{total_acc:+.2f} USDT`"
                        )
                    continue

                # ج. تحديث ومراقبة الوقف المتحرك (Trailing Stop للمرحلة 2)
                if stage == 2:
                    trailing_sl = highest - (1.5 * atr_val)
                    if trailing_sl > sl_p:
                        sl_p = trailing_sl
                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE trades SET stop_loss_price = %s WHERE id = %s;", (sl_p, t_id))
                            conn.commit()

                # د. تنفيذ وقف الخسارة أو كسر الوقف المتحرك
                if cur_price <= sl_p:
                    sell_qty = apply_step_size(sym, rem_qty)
                    if sell_qty > 0:
                        exchange.create_market_sell_order(sym, sell_qty)
                    
                    realized_on_exit = (cur_price - buy_p) * sell_qty - (sell_qty * cur_price * TAKER_FEE_RATE)
                    with get_db_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE trades 
                                SET status = 'CLOSED', sell_price = %s, 
                                    net_profit_usd = net_profit_usd + %s, closed_at = CURRENT_TIMESTAMP 
                                WHERE id = %s;
                            """, (cur_price, realized_on_exit, t_id))
                        conn.commit()

                    total_acc = get_cumulative_profit()
                    action_title = "🛑 قطع الخسارة الأولي" if stage == 0 else "🔒 إغلاق وتأمين الأرباح المتبقية (Trailing SL)"
                    send_telegram(
                        f"{action_title} *(#{t_id})*\n"
                        f"• الزوج: `{sym}`\n"
                        f"• سعر الخروج: `{cur_price:.8g}$`\n"
                        f"• صافي العملية النهائية: `{realized_on_exit:+.2f} USDT`\n"
                        f"• إجمالي أرباح المحفظة: `{total_acc:+.2f} USDT`"
                    )

            # 2. فحص الدخول في فرص سوينغ جديدة (1h)
            open_trades = fetch_active_trades()
            active_symbols = set(t['symbol'] for t in open_trades)
            current_allocated_capital = sum(t['cost'] for t in open_trades)

            can_enter = (
                len(open_trades) < MAX_SLOTS and 
                (current_allocated_capital + FIXED_TRADE_USD) <= MAX_TOTAL_CAPITAL and 
                usdt_free >= FIXED_TRADE_USD
            )

            if can_enter:
                if not is_btc_bullish_swing():
                    time.sleep(15)
                    continue

                for symbol in WATCHLIST:
                    if symbol in active_symbols:
                        continue

                    data = get_swing_indicators(symbol)
                    time.sleep(0.1)

                    if not data:
                        continue

                    # شروط الدخول السوينغ:
                    # 1. إغلاق السعر عند أو تحت الحد السفلي للبولنجر (1h)
                    # 2. مؤشر RSI في ذروة البيع (أقل من العتبة)
                    if data['cur_close'] <= data['lower_band'] and data['rsi'] <= RSI_THRESHOLD:
                        cur_close = data['cur_close']
                        atr_val = data['atr']
                        amount_to_buy = apply_step_size(symbol, FIXED_TRADE_USD / cur_close)
                        active_symbols.add(symbol)

                        buy_order = exchange.create_market_buy_order(symbol, amount_to_buy)
                        entry_price = float(buy_order.get('average') or cur_close)
                        actual_cost = entry_price * amount_to_buy

                        # تحديد وقف الخسارة الأولي عند Entry - (1.5 * ATR)
                        initial_sl = entry_price - (1.5 * atr_val)

                        with get_db_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO trades (
                                        symbol, status, buy_price, quantity, cost_usd, 
                                        stage, atr, remaining_qty, highest_price, stop_loss_price
                                    )
                                    VALUES (%s, 'OPEN', %s, %s, %s, 0, %s, %s, %s, %s) RETURNING id;
                                """, (symbol, entry_price, amount_to_buy, actual_cost, atr_val, amount_to_buy, entry_price, initial_sl))
                                t_new_id = cur.fetchone()[0]
                            conn.commit()

                        tp1_target = entry_price + 1.5 * atr_val
                        tp2_target = entry_price + 3.0 * atr_val

                        send_telegram(
                            f"💎 *اقتناص فرصة سوينغ كبرى (#{t_new_id})*\n"
                            f"• الزوج: `{symbol}` (فريم 1h)\n"
                            f"• سعر الدخول: `{entry_price:.8g}$`\n"
                            f"• مؤشر التشبع البيعي: `RSI = {data['rsi']:.1f}`\n"
                            f"• قيمة التذبذب ATR: `{atr_val:.6g}$`\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎯 *الهدف الأول TP1:* `{tp1_target:.8g}$` (+{((tp1_target-entry_price)/entry_price)*100:.2f}%)\n"
                            f"🚀 *الهدف الثاني TP2:* `{tp2_target:.8g}$` (+{((tp2_target-entry_price)/entry_price)*100:.2f}%)\n"
                            f"🛑 *وقف الخسارة الأولي:* `{initial_sl:.8g}$` (-{((entry_price-initial_sl)/entry_price)*100:.2f}%)\n"
                            f"• رأس المال المستثمر: `{actual_cost:.2f}$`"
                        )
                        break

            time.sleep(10)

        except Exception as loop_err:
            print(f"خطأ الدورة الرئيسية: {loop_err}")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
