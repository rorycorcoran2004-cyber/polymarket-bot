"""
Polymarket Kyle's Lambda Bot
Watches real order flow. When a large order moves price more than
expected (high lambda), someone informed just bet. We copy them.
"""

import json, time, logging, random, requests, sys
from datetime import datetime
from collections import deque

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

LAMBDA_THRESHOLD = 0.000005
MIN_ORDER_SIZE   = 100
BET_AMOUNT       = 5.0
SCAN_INTERVAL    = 20

# Broad keyword list to catch all market phrasings
CRYPTO_KEYWORDS = [
    "btc", "bitcoin", "eth", "ethereum", "sol", "solana",
    "xrp", "ripple", "crypto", "bnb", "doge", "dogecoin",
    "higher", "lower", "above", "below", "price", "up or down"
]

class PaperWallet:
    def __init__(self, balance=500.0):
        self.balance = balance
        self.start   = balance
        self.wins = self.losses = 0

    def bet(self, direction, question, amount, lam, size):
        if amount > self.balance:
            return
        self.balance -= amount
        won = random.random() < 0.54
        payout = amount * (1.85 if won else 0)
        pnl = payout - amount
        self.balance += payout
        self.wins   += 1 if won else 0
        self.losses += 0 if won else 1
        emoji = "✅ WIN" if won else "❌ LOSS"
        log.info(f"  {emoji} | {direction} | ${amount:.2f} → ${pnl:+.2f} | Balance=${self.balance:.2f}")
        log.info(f"       λ={lam:.8f} on ${size:.0f} order")
        log.info(f"       Market: {question[:55]}")

    def summary(self):
        total = self.wins + self.losses
        wr  = f"{self.wins/total:.0%}" if total else "n/a"
        roi = (self.balance - self.start) / self.start
        log.info(f"\n{'='*55}")
        log.info(f"  💰 Balance: ${self.balance:.2f} | ROI: {roi:+.1%}")
        log.info(f"  🎯 Win rate: {wr} ({self.wins}W/{self.losses}L/{total} trades)")
        log.info(f"{'='*55}\n")

def get_markets() -> list[dict]:
    """Fetch active markets — no tag filter so we get everything."""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 50},
            timeout=12,
        )
        all_markets = r.json()

        # Filter locally using broad keyword list
        crypto = []
        for m in all_markets:
            q = m.get("question", "").lower()
            if any(kw in q for kw in CRYPTO_KEYWORDS):
                crypto.append(m)

        log.info(f"  📡 {len(all_markets)} total markets → {len(crypto)} crypto-related")

        # Debug: show what we found
        for m in crypto[:5]:
            log.info(f"     ✓ {m.get('question','')[:60]}")

        return crypto
    except Exception as e:
        log.error(f"  ❌ Market fetch failed: {e}")
        return []

def get_recent_trades(token_id: str) -> list[dict]:
    try:
        r = requests.get(
            "https://clob.polymarket.com/trades",
            params={"token_id": token_id, "limit": 20},
            timeout=8,
        )
        data = r.json()
        if isinstance(data, dict):
            return data.get("data", [])
        return []
    except:
        return []

def get_book(token_id: str) -> tuple:
    """Returns (ask, bid) prices."""
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8,
        )
        data = r.json()
        asks = data.get("asks", [])
        bids = data.get("bids", [])
        ask = float(asks[0]["price"]) if asks else None
        bid = float(bids[0]["price"]) if bids else None
        return ask, bid
    except:
        return None, None

def calc_lambda(trades: list[dict]) -> tuple[float, float, str]:
    """
    Kyle's Lambda: λ = ΔP / Q
    Returns (lambda, order_size_usdc, direction)
    """
    if len(trades) < 2:
        return 0.0, 0.0, ""
    try:
        latest = trades[0]   # most recent
        prev   = trades[1]

        price_now  = float(latest.get("price", 0))
        price_prev = float(prev.get("price", 0))
        size_shares = float(latest.get("size", 0))
        size_usdc   = size_shares * price_now

        if size_usdc < MIN_ORDER_SIZE or price_prev == 0:
            return 0.0, 0.0, ""

        delta_p = abs(price_now - price_prev)
        lam = delta_p / size_usdc
        direction = "YES" if price_now > price_prev else "NO"
        return lam, size_usdc, direction
    except:
        return 0.0, 0.0, ""

def run():
    log.info("="*55)
    log.info("  🤖  Polymarket Kyle's Lambda Bot")
    log.info("  Strategy: Follow informed order flow")
    log.info("  Mode: PAPER — no real money")
    log.info(f"  λ threshold: {LAMBDA_THRESHOLD} | Min order: ${MIN_ORDER_SIZE}")
    log.info("="*55)

    wallet = PaperWallet()
    alerted = set()
    last_trade = 0
    scan_count = 0

    while True:
        try:
            scan_count += 1
            log.info(f"\n🔍 Scan #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")

            markets = get_markets()
            signals  = 0

            for market in markets:
                q      = market.get("question", "")
                tokens = market.get("tokens", [])

                for tok in tokens:
                    tid    = tok.get("token_id", "")
                    label  = tok.get("outcome", "")
                    if not tid:
                        continue

                    trades = get_recent_trades(tid)
                    if not trades:
                        continue

                    lam, size, direction = calc_lambda(trades)

                    if lam >= LAMBDA_THRESHOLD and size >= MIN_ORDER_SIZE:
                        alert_key = f"{tid}_{trades[0].get('id','')}"
                        if alert_key in alerted:
                            continue
                        alerted.add(alert_key)
                        signals += 1

                        ask, bid = get_book(tid)
                        log.info(f"\n  🚨 INFORMED FLOW!")
                        log.info(f"     {q[:55]}")
                        log.info(f"     Token  : {label}")
                        log.info(f"     Signal : {direction}")
                        log.info(f"     λ      : {lam:.8f}")
                        log.info(f"     Order  : ${size:.0f} USDC")
                        log.info(f"     Price  : ask={ask} bid={bid}")

                        if time.time() - last_trade > 30:
                            wallet.bet(direction, q, BET_AMOUNT, lam, size)
                            last_trade = time.time()
                        else:
                            log.info(f"     ⏳ Cooldown — skipping")

            if signals == 0:
                log.info("  — No informed flow this scan")

            wallet.summary()
            log.info(f"⏱  Next scan in {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("\n👋 Stopped")
            wallet.summary()
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
