"""
Polymarket Kyle's Lambda Bot
==============================
Watches real order flow on Polymarket.
When a large order moves the price more than expected (high λ),
it means an informed trader just bet with confidence.
We copy their position automatically.

Kyle's Lambda: λ = ΔP / Q
  ΔP = price change after the order
  Q  = size of the order in USDC
  
High λ = smart money signal → copy the trade
Low  λ = noise → ignore
"""

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

# ── Config ────────────────────────────────────────────────────────────
LAMBDA_THRESHOLD  = 0.000005   # minimum λ to consider "informed" trade
MIN_ORDER_SIZE    = 200        # ignore orders smaller than $200 (noise)
BET_AMOUNT        = 5.0        # paper $ per trade
COPY_WINDOW_SEC   = 60         # seconds to watch for follow-up confirmation
MAX_MARKETS       = 30         # how many markets to monitor at once
SCAN_INTERVAL     = 20         # seconds between full scans

ASSET_KEYWORDS = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "eth"],
    "SOL": ["solana", "sol"], "XRP": ["xrp", "ripple"],
}

# ── Paper Wallet ──────────────────────────────────────────────────────
class PaperWallet:
    def __init__(self, balance=500.0):
        self.balance = balance
        self.start   = balance
        self.wins = self.losses = 0
        self.trades = []

    def bet(self, direction, market, amount, reason):
        if amount > self.balance:
            return
        self.balance -= amount
        # Simulate outcome — real version would wait for resolution
        won = random.random() < 0.54   # slight edge assumed from informed flow
        payout = amount * (1.85 if won else 0)   # ~1.85x on binary market
        pnl = payout - amount
        self.balance += payout
        self.wins   += 1 if won else 0
        self.losses += 0 if won else 1
        emoji = "✅ WIN" if won else "❌ LOSS"
        log.info(f"  {emoji} | {direction} | ${amount:.2f} → ${pnl:+.2f} | Balance=${self.balance:.2f}")
        log.info(f"       Reason: {reason}")
        self.trades.append({"direction": direction, "pnl": pnl, "won": won, "market": market})

    def summary(self):
        total = self.wins + self.losses
        wr  = f"{self.wins/total:.0%}" if total else "n/a"
        roi = (self.balance - self.start) / self.start
        log.info(f"\n{'='*55}")
        log.info(f"  💰 Balance: ${self.balance:.2f} | ROI: {roi:+.1%}")
        log.info(f"  🎯 Win rate: {wr} ({self.wins}W / {self.losses}L / {total} trades)")
        log.info(f"{'='*55}\n")


# ── Order Flow Tracker ────────────────────────────────────────────────
class OrderFlowTracker:
    """
    Tracks recent trades per market token.
    Calculates Kyle's Lambda when a large order comes in.
    """
    def __init__(self):
        # token_id → deque of {price, size, time}
        self.history: dict[str, deque] = {}
        self.last_price: dict[str, float] = {}

    def record(self, token_id: str, price: float, size: float):
        if token_id not in self.history:
            self.history[token_id] = deque(maxlen=50)
        self.history[token_id].append({
            "price": price, "size": size, "t": time.time()
        })
        self.last_price[token_id] = price

    def kyle_lambda(self, token_id: str) -> tuple[float, float, str]:
        """
        Returns (lambda, order_size, direction) for the most recent large order.
        direction: 'YES' or 'NO'
        """
        if token_id not in self.history or len(self.history[token_id]) < 2:
            return 0.0, 0.0, ""

        trades = list(self.history[token_id])
        latest = trades[-1]
        prev   = trades[-2]

        size = latest["size"]
        if size < MIN_ORDER_SIZE:
            return 0.0, 0.0, ""

        delta_p = abs(latest["price"] - prev["price"])
        if delta_p == 0 or size == 0:
            return 0.0, 0.0, ""

        lam = delta_p / size
        direction = "YES" if latest["price"] > prev["price"] else "NO"
        return lam, size, direction


# ── Polymarket API ────────────────────────────────────────────────────
def get_markets(limit=MAX_MARKETS) -> list[dict]:
    """Fetch active crypto prediction markets."""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false",
                    "tag_slug": "crypto", "limit": limit},
            timeout=12,
        )
        markets = r.json()
        log.info(f"  📡 {len(markets)} markets fetched")
        return markets
    except Exception as e:
        log.error(f"  ❌ Market fetch failed: {e}")
        return []


def get_recent_trades(token_id: str) -> list[dict]:
    """Get recent trades for a token from CLOB."""
    try:
        r = requests.get(
            f"https://clob.polymarket.com/trades",
            params={"token_id": token_id, "limit": 20},
            timeout=8,
        )
        return r.json().get("data", [])
    except:
        return []


def get_current_price(token_id: str) -> float | None:
    """Get best ask price for a token."""
    try:
        r = requests.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=8,
        )
        data = r.json()
        asks = data.get("asks", [])
        return float(asks[0]["price"]) if asks else None
    except:
        return None


def classify(question: str) -> str | None:
    q = question.lower()
    for asset, kws in ASSET_KEYWORDS.items():
        if any(k in q for k in kws):
            return asset
    return None


# ── Signal Detector ───────────────────────────────────────────────────
class SignalDetector:
    """
    For each market, fetches recent trades and checks if a large
    informed order just moved the price more than expected.
    """
    def __init__(self):
        self.tracker = OrderFlowTracker()
        self.alerted: set[str] = set()   # avoid double-triggering

    def check_market(self, market: dict) -> dict | None:
        """
        Returns a signal dict if informed flow detected, else None.
        """
        q = market.get("question", "")
        asset = classify(q)
        if not asset:
            return None

        tokens = market.get("tokens", [])
        yes_tok = next((t for t in tokens if t.get("outcome") == "Yes"), None)
        no_tok  = next((t for t in tokens if t.get("outcome") == "No"),  None)
        if not yes_tok or not no_tok:
            return None

        for tok, label in [(yes_tok, "YES"), (no_tok, "NO")]:
            tid = tok.get("token_id", "")
            trades = get_recent_trades(tid)

            if not trades:
                continue

            # Feed trades into tracker
            for trade in reversed(trades):   # oldest first
                try:
                    price = float(trade.get("price", 0))
                    size  = float(trade.get("size", 0)) * price   # convert to USDC
                    self.tracker.record(tid, price, size)
                except:
                    continue

            # Calculate Kyle's Lambda
            lam, size, direction = self.tracker.kyle_lambda(tid)

            if lam >= LAMBDA_THRESHOLD and size >= MIN_ORDER_SIZE:
                alert_key = f"{tid}_{round(lam, 7)}"
                if alert_key in self.alerted:
                    continue
                self.alerted.add(alert_key)

                current_price = self.tracker.last_price.get(tid)
                return {
                    "market_id": market.get("conditionId", ""),
                    "question": q,
                    "asset": asset,
                    "token_id": tid,
                    "token_label": label,
                    "direction": direction,
                    "lambda": lam,
                    "order_size": size,
                    "price": current_price,
                }

        return None


# ── Main Bot ──────────────────────────────────────────────────────────
def run():
    log.info("="*55)
    log.info("  🤖  Polymarket Kyle's Lambda Bot")
    log.info("  Strategy: Copy informed order flow")
    log.info("  Mode: PAPER (no real money)")
    log.info(f"  Min order size: ${MIN_ORDER_SIZE}")
    log.info(f"  Lambda threshold: {LAMBDA_THRESHOLD}")
    log.info("="*55)

    wallet   = PaperWallet(balance=500.0)
    detector = SignalDetector()
    scan_count = 0
    last_trade = 0

    while True:
        try:
            scan_count += 1
            log.info(f"\n🔍 Scan #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")

            markets = get_markets()
            signals_found = 0

            for market in markets:
                signal = detector.check_market(market)

                if signal:
                    signals_found += 1
                    lam   = signal["lambda"]
                    size  = signal["order_size"]
                    price = signal["price"]
                    q     = signal["question"]
                    direction = signal["direction"]
                    asset = signal["asset"]

                    log.info(f"\n  🚨 INFORMED FLOW DETECTED!")
                    log.info(f"     Market : {q[:55]}")
                    log.info(f"     Asset  : {asset}")
                    log.info(f"     Signal : {direction}")
                    log.info(f"     λ      : {lam:.8f} (threshold: {LAMBDA_THRESHOLD})")
                    log.info(f"     Order  : ${size:.0f} moved price unusually far")
                    log.info(f"     Price  : {price:.3f}")

                    # Only bet if cooldown has passed
                    if time.time() - last_trade > 30:
                        reason = (f"λ={lam:.8f} on ${size:.0f} order "
                                  f"in '{q[:40]}'")
                        wallet.bet(direction, q, BET_AMOUNT, reason)
                        last_trade = time.time()
                    else:
                        log.info(f"     ⏳ Skipping — cooldown active")

            if signals_found == 0:
                log.info(f"  — No informed flow detected this scan")
            else:
                log.info(f"  ✓ {signals_found} signal(s) found this scan")

            wallet.summary()
            log.info(f"⏱  Next scan in {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("\n👋 Bot stopped")
            wallet.summary()
            break
        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run()
