import json, time, logging, random, requests, sys
from datetime import datetime
from collections import deque
import threading, websocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

ASSET_PAIRS = {"BTC": "XBT/USD", "ETH": "ETH/USD", "SOL": "SOL/USD", "XRP": "XRP/USD"}
ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"], "XRP": ["xrp", "ripple"],
}

class PaperWallet:
    def __init__(self, balance=500.0):
        self.balance = balance
        self.start_balance = balance
        self.trades = []
        self.wins = 0
        self.losses = 0

    def bet(self, direction, asset, question, price, amount):
        if amount > self.balance:
            log.info("  ⚠️  Not enough balance")
            return
        self.balance -= amount
        won = random.random() < price
        payout = (amount / price) if won else 0
        pnl = payout - amount
        self.balance += payout
        self.wins += (1 if won else 0)
        self.losses += (0 if won else 1)
        emoji = "✅ WIN" if won else "❌ LOSS"
        log.info(f"  {emoji} | {direction} {asset} | ${amount:.2f} → ${pnl:+.2f} | Balance=${self.balance:.2f}")
        self.trades.append({"asset": asset, "direction": direction, "pnl": pnl, "won": won})

    def summary(self):
        total = self.wins + self.losses
        wr = f"{self.wins/total:.0%}" if total > 0 else "n/a"
        roi = (self.balance - self.start_balance) / self.start_balance
        log.info(f"\n{'='*50}")
        log.info(f"  💰 Balance: ${self.balance:.2f} (started ${self.start_balance:.2f})")
        log.info(f"  📈 ROI: {roi:+.1%}")
        log.info(f"  🎯 Win rate: {wr} ({self.wins}W / {self.losses}L)")
        log.info(f"  📊 Total trades: {total}")
        log.info(f"{'='*50}\n")

class PriceFeed:
    def __init__(self):
        self.prices = {a: deque(maxlen=500) for a in ASSET_PAIRS}
        self.current = {a: None for a in ASSET_PAIRS}

    def start(self):
        def on_message(ws, raw):
            try:
                data = json.loads(raw)
                if not isinstance(data, list) or len(data) < 4:
                    return
                ticker = data[1]
                pair = data[3]
                mapping = {"XBT/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL", "XRP/USD": "XRP"}
                asset = mapping.get(pair)
                if asset and isinstance(ticker, dict):
                    p = float(ticker.get("c", [0])[0] or 0)
                    if p > 0:
                        self.current[asset] = p
                        self.prices[asset].append({"p": p, "t": time.time()})
            except Exception as e:
                pass

        def connect():
            ws = websocket.WebSocketApp(
                "wss://ws.kraken.com",
                on_open=lambda ws: ws.send(json.dumps({
                    "event": "subscribe",
                    "pair": ["XBT/USD", "ETH/USD", "SOL/USD", "XRP/USD"],
                    "subscription": {"name": "ticker"}
                })),
                on_message=on_message,
                on_close=lambda ws, *a: (time.sleep(3), connect()),
            )
            ws.run_forever(ping_interval=20)

        threading.Thread(target=connect, daemon=True).start()

    def momentum(self, asset, window_sec=60):
        history = list(self.prices[asset])
        if len(history) < 3:
            return None
        recent = [p for p in history if p["t"] >= time.time() - window_sec]
        if len(recent) < 3:
            return None
        return (recent[-1]["p"] - recent[0]["p"]) / recent[0]["p"] * 100

    def is_ready(self):
        return all(v is not None for v in self.current.values())

def get_markets():
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "tag_slug": "crypto"},
            timeout=12,
        )
        return r.json()
    except Exception as e:
        log.error(f"Market fetch failed: {e}")
        return []

def classify(question):
    q = question.lower()
    asset = next((a for a, kws in ASSET_KEYWORDS.items() if any(k in q for k in kws)), None)
    if not asset:
        return None, 0
    if "5" in q and "min" in q:
        return asset, 5
    if "15" in q and "min" in q:
        return asset, 15
    return None, 0

def get_prices(token_id):
    try:
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=8)
        data = r.json()
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        ask = float(asks[0]["price"]) if asks else None
        bid = float(bids[0]["price"]) if bids else None
        return ask, bid
    except:
        return None, None

def run():
    log.info("="*50)
    log.info("  🤖  Polymarket Paper Trading Bot")
    log.info("  Mode: PAPER (no real money)")
    log.info("  Price feed: Kraken")
    log.info("="*50)

    wallet = PaperWallet(balance=500.0)
    feed = PriceFeed()
    feed.start()

    log.info("⏳ Warming up price feeds (15s)...")
    time.sleep(15)

    scan_count = 0
    last_trade = 0

    while True:
        try:
            scan_count += 1
            log.info(f"\n🔍 Scan #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")

            if not feed.is_ready():
                log.info("  ⏳ Waiting for prices...")
                time.sleep(10)
                continue

            for asset in ASSET_PAIRS:
                price = feed.current[asset]
                mom = feed.momentum(asset, 60)
                if price:
                    mom_str = f"{mom:+.2f}%" if mom else "n/a"
                    log.info(f"  {asset}: ${price:,.2f} ({mom_str} 1m)")

            markets = get_markets()
            best_opp = None
            best_edge = 0

            for m in markets:
                q = m.get("question", "")
                asset, tf = classify(q)
                if not asset:
                    continue
                mom = feed.momentum(asset, 60)
                if mom is None:
                    continue
                tokens = m.get("tokens", [])
                yes_tok = next((t for t in tokens if t.get("outcome") == "Yes"), None)
                no_tok  = next((t for t in tokens if t.get("outcome") == "No"), None)
                if not yes_tok or not no_tok:
                    continue
                yes_ask, _ = get_prices(yes_tok["token_id"])
                no_ask,  _ = get_prices(no_tok["token_id"])
                if not yes_ask or not no_ask:
                    continue
                hedge_profit = 1.0 - (yes_ask + no_ask)
                if hedge_profit > 0.02:
                    log.info(f"  🔒 HEDGE found! π={hedge_profit:.4f} | {q[:50]}")
                    if time.time() - last_trade > 30:
                        wallet.bet("HEDGE-YES", asset, q, yes_ask, 5.0)
                        wallet.bet("HEDGE-NO",  asset, q, no_ask,  5.0)
                        last_trade = time.time()
                    continue
                decay = 1.0 / (tf ** 0.55)
                shift = max(-0.30, min(0.30, mom * decay * 0.10))
                p_yes = 0.50 + shift
                p_no  = 1.0 - p_yes
                yes_edge = p_yes - yes_ask
                no_edge  = p_no  - no_ask
                if yes_edge > best_edge and yes_edge > 0.06:
                    best_edge = yes_edge
                    best_opp = {"dir": "YES", "asset": asset, "q": q, "price": yes_ask, "edge": yes_edge}
                elif no_edge > best_edge and no_edge > 0.06:
                    best_edge = no_edge
                    best_opp = {"dir": "NO", "asset": asset, "q": q, "price": no_ask, "edge": no_edge}

            if best_opp and time.time() - last_trade > 30:
                log.info(f"  🎯 Best opportunity: {best_opp['dir']} {best_opp['asset']} | edge={best_opp['edge']:.3f}")
                wallet.bet(best_opp["dir"], best_opp["asset"], best_opp["q"], best_opp["price"], 5.0)
                last_trade = time.time()
            else:
                log.info("  — No strong opportunity this scan")

            wallet.summary()
            log.info("⏱  Next scan in 30s...")
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("\n👋 Bot stopped")
            wallet.summary()
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
