#!/usr/bin/env python3
"""
╔═══════════════════════════════════════╗
║   LIQUIDITY ZONE TRADING BOT v2.1     ║
║   V10(30min) V25(15min) V75(15min)     ║
║   Touches: 10 | Railway Edition        ║
╚═══════════════════════════════════════╝
"""

import websocket
import json
import time
import requests
import logging
import os
from datetime import datetime, timedelta
from collections import deque

# ============================================================
#                  CONFIGURATION
# ============================================================

CONFIG = {
    "deriv_token"      : os.getenv("DERIV_TOKEN", ""),
    "deriv_app_id"     : os.getenv("DERIV_APP_ID", ""),
    "telegram_token"   : os.getenv("TELEGRAM_TOKEN", ""),
    "telegram_chat_id" : os.getenv("TELEGRAM_CHAT_ID", ""),
    "telegram_enabled" : True,

    "instruments": {
        "R_10": {"expiry": 30, "name": "Volatility 10"},
        "R_25": {"expiry": 15, "name": "Volatility 25"},
        "R_75": {"expiry": 15, "name": "Volatility 75"},
    },

    "stake"              : 1.0,
    "cooldown"           : 5,
    "payout"             : 95.0,
    "use_doji"           : True,
    "zz_depth"           : 12,
    "zz_deviation"       : 5,
    "zz_backstep"        : 3,
    "max_touches"        : 10,
    "m15_bars"           : 2880,
    "max_trades_per_day" : 60,
    "daily_stop_loss"    : -15.0,
}

# ============================================================
#                    LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ============================================================
#                  TELEGRAM
# ============================================================

def telegram(message):
    if not CONFIG["telegram_enabled"]:
        return
    if not CONFIG["telegram_token"]:
        return
    try:
        url = f"https://api.telegram.org/bot{CONFIG['telegram_token']}/sendMessage"
        requests.post(url, json={
            "chat_id": CONFIG["telegram_chat_id"],
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        log.error(f"Telegram erreur: {e}")

# ============================================================
#                  STRUCTURES
# ============================================================

class Candle:
    def __init__(self, o, h, l, c, t):
        self.open  = o
        self.high  = h
        self.low   = l
        self.close = c
        self.time  = t

class Zone:
    def __init__(self, high, low, ztype, ctime):
        self.high        = high
        self.low         = low
        self.type        = ztype
        self.create_time = ctime
        self.broken_time = 0
        self.touch_count = 0

class Trade:
    def __init__(self, stime, direction, symbol, price, stake, expiry):
        self.signal_time  = stime
        self.result_time  = 0
        self.direction    = direction
        self.symbol       = symbol
        self.entry_price  = price
        self.exit_price   = 0
        self.stake        = stake
        self.expiry       = expiry
        self.is_win       = False
        self.profit       = 0
        self.contract_id  = None
        self.pattern      = ""

# ============================================================
#                  ZONES S/R
# ============================================================

class ZoneDetector:

    def __init__(self):
        self.depth     = CONFIG["zz_depth"]
        self.backstep  = CONFIG["zz_backstep"]
        self.max_touch = CONFIG["max_touches"]

    def compute_zones(self, m15_candles):
        zones = []
        candles = list(m15_candles)
        count = len(candles)
        if count < 50:
            return zones

        for i in range(self.depth, count - self.backstep):
            is_high = True
            is_low  = True

            for j in range(1, self.depth + 1):
                left  = i - j
                right = i + j
                if left < 0 or right >= count:
                    is_high = False
                    is_low  = False
                    break

                if candles[i].high < candles[left].high or \
                   candles[i].high < candles[right].high:
                    is_high = False
                if candles[i].low > candles[left].low or \
                   candles[i].low > candles[right].low:
                    is_low = False

                if not is_high and not is_low:
                    break

            if is_high:
                zh = candles[i].high
                zl = max(candles[i].open, candles[i].close)
                if zh - zl < 1e-5:
                    zl = zh - 0.001
                zone = Zone(zh, zl, -1, candles[i].time)
                for k in range(i + 1, count):
                    if candles[k].close > zh:
                        zone.broken_time = candles[k].time
                        break
                if not self._is_dup(zone, zones):
                    zones.append(zone)

            if is_low:
                zh = min(candles[i].open, candles[i].close)
                zl = candles[i].low
                if zh - zl < 1e-5:
                    zh = zl + 0.001
                zone = Zone(zh, zl, 1, candles[i].time)
                for k in range(i + 1, count):
                    if candles[k].close < zl:
                        zone.broken_time = candles[k].time
                        break
                if not self._is_dup(zone, zones):
                    zones.append(zone)

        return zones

    def _is_dup(self, sp, zones):
        for z in zones:
            if z.broken_time > 0:
                continue
            if z.type != sp.type:
                continue
            mid1 = (sp.high + sp.low) / 2
            mid2 = (z.high + z.low) / 2
            rng  = ((sp.high - sp.low) + (z.high - z.low)) / 2
            if abs(mid1 - mid2) < rng:
                return True
        return False

    def find_zone(self, candle, zones, now):
        for z in reversed(zones):
            if z.create_time >= now:
                continue
            if z.broken_time > 0 and z.broken_time <= now:
                continue
            if z.touch_count >= self.max_touch:
                continue
            if z.type == 1:
                if candle.low <= z.high and candle.close >= z.low:
                    return z
            if z.type == -1:
                if candle.high >= z.low and candle.close <= z.high:
                    return z
        return None

# ============================================================
#                  PATTERNS
# ============================================================

class Patterns:

    @staticmethod
    def scan(candles, zone_type):
        if len(candles) < 3:
            return 0, ""
        c = candles
        i = len(c) - 1

        if zone_type == 1:
            if Patterns._bull_engulf(c, i):  return 1, "Engulfing Haussier"
            if Patterns._hammer(c, i):       return 1, "Marteau"
            if Patterns._bull_pin(c, i):     return 1, "Pin Bar Haussière"
            if Patterns._morning(c, i):      return 1, "Étoile du Matin"
            if CONFIG["use_doji"] and Patterns._doji(c, i, 1):
                return 1, "Doji Haussier"

        if zone_type == -1:
            if Patterns._bear_engulf(c, i):  return -1, "Engulfing Baissier"
            if Patterns._shooting(c, i):     return -1, "Étoile Filante"
            if Patterns._bear_pin(c, i):     return -1, "Pin Bar Baissière"
            if Patterns._evening(c, i):      return -1, "Étoile du Soir"
            if CONFIG["use_doji"] and Patterns._doji(c, i, -1):
                return -1, "Doji Baissier"

        return 0, ""

    @staticmethod
    def _bull_engulf(c, i):
        pb = abs(c[i-1].close - c[i-1].open)
        cb = abs(c[i].close - c[i].open)
        if pb == 0 or cb == 0: return False
        return (c[i-1].close < c[i-1].open and c[i].close > c[i].open and
                c[i].open <= c[i-1].close and c[i].close >= c[i-1].open and
                cb >= pb * 0.8)

    @staticmethod
    def _hammer(c, i):
        body = abs(c[i].close - c[i].open)
        rng  = c[i].high - c[i].low
        if rng == 0 or body == 0: return False
        lw = min(c[i].open, c[i].close) - c[i].low
        uw = c[i].high - max(c[i].open, c[i].close)
        return lw >= body * 2 and uw <= body * 0.5 and body / rng < 0.4

    @staticmethod
    def _bull_pin(c, i):
        rng = c[i].high - c[i].low
        if rng == 0: return False
        lw = min(c[i].open, c[i].close) - c[i].low
        uw = c[i].high - max(c[i].open, c[i].close)
        return lw / rng >= 0.66 and uw / rng <= 0.15

    @staticmethod
    def _morning(c, i):
        if i < 2: return False
        b1 = abs(c[i-2].close - c[i-2].open)
        b2 = abs(c[i-1].close - c[i-1].open)
        b3 = abs(c[i].close - c[i].open)
        if b1 == 0: return False
        return (c[i-2].close < c[i-2].open and b2 < b1 * 0.4 and
                c[i].close > c[i].open and b3 > b1 * 0.5 and
                c[i].close > (c[i-2].open + c[i-2].close) / 2)

    @staticmethod
    def _bear_engulf(c, i):
        pb = abs(c[i-1].close - c[i-1].open)
        cb = abs(c[i].close - c[i].open)
        if pb == 0 or cb == 0: return False
        return (c[i-1].close > c[i-1].open and c[i].close < c[i].open and
                c[i].open >= c[i-1].close and c[i].close <= c[i-1].open and
                cb >= pb * 0.8)

    @staticmethod
    def _shooting(c, i):
        body = abs(c[i].close - c[i].open)
        rng  = c[i].high - c[i].low
        if rng == 0 or body == 0: return False
        uw = c[i].high - max(c[i].open, c[i].close)
        lw = min(c[i].open, c[i].close) - c[i].low
        return uw >= body * 2 and lw <= body * 0.5 and body / rng < 0.4

    @staticmethod
    def _bear_pin(c, i):
        rng = c[i].high - c[i].low
        if rng == 0: return False
        uw = c[i].high - max(c[i].open, c[i].close)
        lw = min(c[i].open, c[i].close) - c[i].low
        return uw / rng >= 0.66 and lw / rng <= 0.15

    @staticmethod
    def _evening(c, i):
        if i < 2: return False
        b1 = abs(c[i-2].close - c[i-2].open)
        b2 = abs(c[i-1].close - c[i-1].open)
        b3 = abs(c[i].close - c[i].open)
        if b1 == 0: return False
        return (c[i-2].close > c[i-2].open and b2 < b1 * 0.4 and
                c[i].close < c[i].open and b3 > b1 * 0.5 and
                c[i].close < (c[i-2].open + c[i-2].close) / 2)

    @staticmethod
    def _doji(c, i, ztype):
        body = abs(c[i].close - c[i].open)
        rng  = c[i].high - c[i].low
        if rng == 0 or body / rng >= 0.15: return False
        uw = c[i].high - max(c[i].open, c[i].close)
        lw = min(c[i].open, c[i].close) - c[i].low
        if ztype == 1 and lw / rng > 0.45:  return True
        if ztype == -1 and uw / rng > 0.45: return True
        return False

# ============================================================
#                  STATISTIQUES
# ============================================================

class Stats:

    def __init__(self):
        self.results = []

    def add(self, trade):
        self.results.append(trade)
        self.save()

    def calc(self, from_time=0, symbol=None):
        w = l = cw = cl = mw = ml = 0
        for r in self.results:
            if from_time > 0 and r.signal_time < from_time:
                continue
            if symbol and r.symbol != symbol:
                continue
            if r.is_win:
                w += 1; cw += 1; cl = 0
                if cw > mw: mw = cw
            else:
                l += 1; cl += 1; cw = 0
                if cl > ml: ml = cl

        cs = cw if cw > 0 else -cl
        t = w + l
        wr = (w / t * 100) if t > 0 else 0
        p = (w * CONFIG["payout"] / 100) - l
        return {
            "wins": w, "losses": l, "total": t,
            "winrate": wr, "profit": p,
            "streak": cs, "max_w": mw, "max_l": ml
        }

    def today(self, symbol=None):
        t = datetime.now().replace(hour=0, minute=0, second=0)
        return self.calc(t.timestamp(), symbol)

    def week(self, symbol=None):
        now = datetime.now()
        mon = now - timedelta(days=now.weekday())
        t = mon.replace(hour=0, minute=0, second=0)
        return self.calc(t.timestamp(), symbol)

    def month(self, symbol=None):
        t = datetime.now().replace(day=1, hour=0, minute=0, second=0)
        return self.calc(t.timestamp(), symbol)

    def format_all(self):
        d = self.today()
        w = self.week()
        m = self.month()
        t = self.calc()

        def fmt(s, label):
            sk = (f"{s['streak']}W" if s['streak'] > 0
                  else f"{abs(s['streak'])}L" if s['streak'] < 0
                  else "0")
            return (
                f"📊 <b>{label}</b>\n"
                f"   W:{s['wins']} L:{s['losses']} | "
                f"WR: {s['winrate']:.1f}%\n"
                f"   Profit: {s['profit']:.2f}$ "
                f"({s['total']} trades)\n"
                f"   Série: {sk} | "
                f"MaxW:{s['max_w']} MaxL:{s['max_l']}"
            )

        def fmt_sym(sym):
            info = CONFIG["instruments"][sym]
            s = self.month(sym)
            if s["total"] == 0:
                return f"   {info['name']}: Aucun trade"
            sk = (f"{s['streak']}W" if s['streak'] > 0
                  else f"{abs(s['streak'])}L" if s['streak'] < 0
                  else "0")
            return (
                f"   {info['name']} ({info['expiry']}min):\n"
                f"      WR:{s['winrate']:.1f}% | "
                f"{s['total']}t | +{s['profit']:.2f}$\n"
                f"      Série:{sk} MaxW:{s['max_w']} MaxL:{s['max_l']}"
            )

        msg  = "━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "🎯 <b>LZ TRADING BOT v2.1</b>\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        msg += fmt(d, "AUJOURD'HUI") + "\n\n"
        msg += fmt(w, "CETTE SEMAINE") + "\n\n"
        msg += fmt(m, "CE MOIS") + "\n\n"
        msg += fmt(t, "TOTAL") + "\n\n"
        msg += "📌 <b>PAR INSTRUMENT (mois)</b>\n"
        for sym in CONFIG["instruments"]:
            msg += fmt_sym(sym) + "\n"
        msg += f"\n💰 Mise: {CONFIG['stake']}$\n"
        msg += "━━━━━━━━━━━━━━━━━━━━━━━━━"
        return msg

    def save(self):
        try:
            data = []
            for r in self.results:
                data.append({
                    "time": r.signal_time,
                    "dir": r.direction,
                    "symbol": r.symbol,
                    "win": r.is_win,
                    "profit": r.profit,
                    "expiry": r.expiry
                })
            with open("bot_stats.json", "w") as f:
                json.dump(data, f)
        except:
            pass

    def load(self):
        try:
            if not os.path.exists("bot_stats.json"):
                return
            with open("bot_stats.json", "r") as f:
                data = json.load(f)
            for d in data:
                t = Trade(d["time"], d["dir"], d["symbol"],
                          0, CONFIG["stake"], d.get("expiry", 5))
                t.is_win = d["win"]
                t.profit = d["profit"]
                self.results.append(t)
            log.info(f"📂 {len(self.results)} résultats chargés")
        except:
            pass

# ============================================================
#                  BOT PRINCIPAL
# ============================================================

class TradingBot:

    def __init__(self):
        self.ws = None
        self.authorized = False
        self.symbols = list(CONFIG["instruments"].keys())

        self.m15 = {}
        self.m1  = {}
        self.zones = {}
        self.last_sig = {}
        self.m15_ok = {}
        self.m1_ok  = {}

        for sym in self.symbols:
            self.m15[sym]      = deque(maxlen=CONFIG["m15_bars"])
            self.m1[sym]       = deque(maxlen=500)
            self.zones[sym]    = []
            self.last_sig[sym] = 0
            self.m15_ok[sym]   = False
            self.m1_ok[sym]    = False

        self.pending_trades = {}
        self._req_id = 0
        self.open_trades = {}

        self.stats = Stats()
        self.stats.load()
        self.daily_profit = 0
        self.daily_trades = 0
        self.last_day = datetime.now().day

        self.zd = ZoneDetector()

    def run(self):
        log.info("🚀 LZ Trading Bot v2.1")

        for sym in self.symbols:
            info = CONFIG["instruments"][sym]
            log.info(f"📊 {info['name']} | Expiry: {info['expiry']}min")

        log.info(f"💰 Mise: {CONFIG['stake']}$")
        log.info(f"🔄 Touches max: {CONFIG['max_touches']}")

        inst_list = ""
        for sym in self.symbols:
            info = CONFIG["instruments"][sym]
            inst_list += f"  • {info['name']}: {info['expiry']}min\n"

        telegram(
            f"🚀 <b>LZ Trading Bot v2.1 démarré</b>\n"
            f"☁️ Railway\n\n"
            f"📊 <b>Instruments:</b>\n{inst_list}\n"
            f"💰 Mise: {CONFIG['stake']}$\n"
            f"🔄 Touches: {CONFIG['max_touches']}\n"
            f"⚡ Trades simultanés: OUI"
        )

        url = (f"wss://ws.binaryws.com/websockets/v3"
               f"?app_id={CONFIG['deriv_app_id']}")

        while True:
            try:
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_msg,
                    on_error=self._on_err,
                    on_close=self._on_close
                )
                self.ws.run_forever(ping_interval=30,
                                    ping_timeout=10)
            except Exception as e:
                log.error(f"❌ {e}")

            log.info("🔄 Reconnexion dans 5s...")
            time.sleep(5)

    def _on_open(self, ws):
        log.info("🔌 Connecté à Deriv")
        ws.send(json.dumps({"authorize": CONFIG["deriv_token"]}))

    def _on_err(self, ws, error):
        log.error(f"❌ WS: {error}")

    def _on_close(self, ws, code, msg):
        log.info(f"🔌 Déconnecté ({code})")
        self.authorized = False

    def _on_msg(self, ws, message):
        try:
            data = json.loads(message)
            if "error" in data:
                log.error(f"❌ API: {data['error']['message']}")
                return

            mt = data.get("msg_type", "")
            if mt == "authorize":       self._auth(data)
            elif mt == "candles":       self._hist(data)
            elif mt == "ohlc":          self._ohlc(data)
            elif mt == "buy":           self._bought(data)
            elif mt == "proposal_open_contract": self._contract(data)

        except Exception as e:
            log.error(f"❌ Message: {e}")

    def _auth(self, data):
        info = data["authorize"]
        log.info(f"✅ {info['fullname']} | Solde: {info['balance']}$")
        self.authorized = True

        telegram(f"✅ <b>Connecté</b>\n"
                 f"Compte: {info['fullname']}\n"
                 f"Solde: {info['balance']}$")

        for sym in self.symbols:
            self.ws.send(json.dumps({
                "ticks_history": sym, "style": "candles",
                "granularity": 900, "count": CONFIG["m15_bars"],
                "subscribe": 1
            }))
            self.ws.send(json.dumps({
                "ticks_history": sym, "style": "candles",
                "granularity": 60, "count": 200,
                "subscribe": 1
            }))

    def _hist(self, data):
        req = data.get("echo_req", {})
        sym = req.get("ticks_history", "")
        gran = req.get("granularity", 60)
        if sym not in self.symbols: return

        for c in data.get("candles", []):
            candle = Candle(float(c["open"]), float(c["high"]),
                           float(c["low"]), float(c["close"]),
                           int(c["epoch"]))
            if gran == 900: self.m15[sym].append(candle)
            else:           self.m1[sym].append(candle)

        info = CONFIG["instruments"][sym]
        if gran == 900:
            self.m15_ok[sym] = True
            self.zones[sym] = self.zd.compute_zones(self.m15[sym])
            act = sum(1 for z in self.zones[sym] if z.broken_time == 0)
            log.info(f"📍 {info['name']} | {len(self.zones[sym])} zones ({act} actives)")
        else:
            self.m1_ok[sym] = True
            log.info(f"📊 {info['name']} | M1 prêt ({len(self.m1[sym])} bougies)")

    def _ohlc(self, data):
        ohlc = data.get("ohlc", {})
        sym  = ohlc.get("symbol", "")
        gran = int(ohlc.get("granularity", 60))
        if sym not in self.symbols: return

        candle = Candle(float(ohlc["open"]), float(ohlc["high"]),
                       float(ohlc["low"]), float(ohlc["close"]),
                       int(ohlc["open_time"]))

        if gran == 900:
            buf = self.m15[sym]
            if buf and candle.time != buf[-1].time:
                buf.append(candle)
                self.zones[sym] = self.zd.compute_zones(buf)
            elif buf:
                buf[-1] = candle
        elif gran == 60:
            buf = self.m1[sym]
            if buf and candle.time != buf[-1].time:
                buf.append(candle)
                self._check_signal(sym)
            elif buf:
                buf[-1] = candle

    def _check_signal(self, sym):
        if datetime.now().day != self.last_day:
            self.daily_profit = 0
            self.daily_trades = 0
            self.last_day = datetime.now().day
            telegram("🔄 <b>Nouveau jour</b>\n\n" + self.stats.format_all())

        if not self.m15_ok.get(sym) or not self.m1_ok.get(sym): return
        if self.daily_trades >= CONFIG["max_trades_per_day"]: return
        if self.daily_profit <= CONFIG["daily_stop_loss"]: return

        now = time.time()
        if now - self.last_sig.get(sym, 0) < CONFIG["cooldown"] * 60: return

        candles = list(self.m1[sym])
        if len(candles) < 3: return

        current = candles[-1]
        zone = self.zd.find_zone(current, self.zones[sym], current.time)
        if zone is None: return

        direction, pattern = Patterns.scan(candles, zone.type)
        if direction == 0: return

        zone.touch_count += 1
        self.last_sig[sym] = now
        ctype = "CALL" if direction == 1 else "PUT"
        info = CONFIG["instruments"][sym]
        log.info(f"🎯 {ctype} {info['name']} | {pattern} | {info['expiry']}min")
        self._trade(sym, ctype, current.close, pattern)

    def _trade(self, sym, ctype, price, pattern):
        info = CONFIG["instruments"][sym]
        expiry = info["expiry"]
        trade = Trade(time.time(), ctype, sym, price, CONFIG["stake"], expiry)
        trade.pattern = pattern

        self._req_id += 1
        req_id = self._req_id
        self.pending_trades[req_id] = trade

        self.ws.send(json.dumps({
            "buy": 1, "price": CONFIG["stake"],
            "parameters": {
                "contract_type": ctype, "currency": "USD",
                "amount": CONFIG["stake"], "basis": "stake",
                "symbol": sym, "duration": expiry, "duration_unit": "m"
            },
            "req_id": req_id
        }))

        emoji = "🟢" if ctype == "CALL" else "🔴"
        active = len(self.open_trades) + len(self.pending_trades)
        telegram(f"{emoji} <b>SIGNAL {ctype}</b>\n"
                 f"📌 {info['name']}\n📐 {pattern}\n"
                 f"💵 Prix: {price}\n💰 Mise: {CONFIG['stake']}$\n"
                 f"⏱ Expiry: {expiry} min\n📊 Trades actifs: {active}")

    def _bought(self, data):
        buy = data.get("buy", {})
        cid = buy.get("contract_id")
        req_id = data.get("req_id")

        if req_id in self.pending_trades and cid:
            trade = self.pending_trades[req_id]
            trade.contract_id = cid
            self.open_trades[cid] = trade
            self.daily_trades += 1
            info = CONFIG["instruments"][trade.symbol]
            log.info(f"📝 Trade ouvert | {info['name']} | ID: {cid}")

            self.ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": cid, "subscribe": 1
            }))
            del self.pending_trades[req_id]

    def _contract(self, data):
        poc = data.get("proposal_open_contract", {})
        cid = poc.get("contract_id")
        if not poc.get("is_sold") or cid not in self.open_trades: return

        trade = self.open_trades[cid]
        profit = float(poc.get("profit", 0))
        trade.is_win = profit > 0
        trade.profit = profit
        trade.exit_price = float(poc.get("sell_price", 0))
        trade.result_time = time.time()

        self.stats.add(trade)
        self.daily_profit += profit
        info = CONFIG["instruments"][trade.symbol]

        if trade.is_win:
            log.info(f"✅ WIN +{profit:.2f}$ | {info['name']} {trade.direction}")
        else:
            log.info(f"❌ LOSS {profit:.2f}$ | {info['name']} {trade.direction}")

        day = self.stats.today()
        sk = (f"{day['streak']}W" if day['streak'] > 0
              else f"{abs(day['streak'])}L" if day['streak'] < 0 else "0")

        emoji = "✅" if trade.is_win else "❌"
        pstr = f"+{profit:.2f}" if profit > 0 else f"{profit:.2f}"
        telegram(f"{emoji} <b>{'WIN' if trade.is_win else 'LOSS'} {pstr}$</b>\n"
                 f"{info['name']} | {trade.direction} | {trade.expiry}min\n"
                 f"Série: {sk} | WR: {day['winrate']:.1f}%\n"
                 f"Profit jour: {day['profit']:.2f}$ ({day['total']} trades)")

        if day["total"] % 10 == 0 and day["total"] > 0:
            telegram(self.stats.format_all())

        del self.open_trades[cid]
        sub_id = data.get("subscription", {}).get("id")
        if sub_id:
            self.ws.send(json.dumps({"forget": sub_id}))

# ============================================================
#                  LANCEMENT
# ============================================================

if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════╗
    ║   🎯 LIQUIDITY ZONE TRADING BOT v2.1  ║
    ║                                        ║
    ║   V10 → 30min expiry                  ║
    ║   V25 → 15min expiry                  ║
    ║   V75 → 15min expiry                  ║
    ║                                        ║
    ║   Touches: 10 | Doji: ON             ║
    ║   Trades simultanés: OUI             ║
    ╚═══════════════════════════════════════╝
    """)

    bot = TradingBot()
    bot.run()
