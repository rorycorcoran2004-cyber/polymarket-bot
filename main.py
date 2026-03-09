"""
Polymarket Kyle's Lambda Bot - Debug build
Prints ALL market questions so we can see what's actually available.
"""

import time, logging, random, requests, sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

LAMBDA_THRESHOLD = 0.000003
MIN_ORDER_SIZE   = 50
BET_AMOUNT       = 5.0
SCAN_INTERVAL    = 30

CRYPTO_KEYWORDS = [
    "btc", "bitcoin", "eth", "ethereum", "sol", "solana",
    "xrp", "ripple", "crypto", "bnb", "doge", "dogecoin",
    "higher", "lower", "above", "below", "up or down",
    "price", "usd", "token", "coin", "market", "rally",
    "pump", "dump", "bull", "bear", "trade", "exchange"
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
        log.info(f"       λ={lam:.8f} | order=${size:.0f} | {question[:50]}")

    def summary(self):
        total = self.wins + self.losses
        wr  = f"{self.wins/total:.0%}" if total else "n/a"
        roi = (self.balance - self.start) / self.start
        log.info(f"\n{'='*55}")
        log.info(f"  💰 Balance: ${self.balance:.2f} | ROI: {roi:+.1%}")
        log.info(f"  🎯 Win rate: {wr} ({self.wins}W/{self.losses}L/{total} trades)")
        log.info(f"{'='*55}\n")


def get_markets() -> list[dict]:
    """
    Try multiple API endpoints and params to find active markets.
    Print ALL questions so we can see what's there.
    """
    all_markets = {}

    # Try 1: no filters at all, just get latest active markets
    endpoints = [
        {"active": "true", "closed": "false", "limit": 100},
        {"active": "true", "closed": "false", "limit": 100, "order": "volume", "ascending": "false"},
        {"active": "true", "closed": "false", "limit": 100, "order": "startDate", "ascending": "false"},
    ]

    for params in endpoints:
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params=params,
                timeout=12,
            )
            for m in r.json():
                mid = m.get("conditionId", "")
                if mid:
                    all_markets[mid] = m
            time.sleep(0.3)
        except Exception as e:
            log.error(f"  ❌ Fetch failed: {e}")

    markets = list(all_markets.values())
    log.info(f"  📡 {len(markets)} total markets fetched")

    # Print ALL questions so we can see what's available
    log.info("  📋 ALL MARKET QUESTIONS:")
    for m in markets:
        log.info(f"     | {m.get('question','')[:70]}")

    # Filter for crypto
    crypto = [m for m in markets
              if any(kw in m.get("question","").lower() for kw in CRYPTO_KEYWORDS)]

    log.info(f"  🔍 {len(crypto)} matched crypto keywords")
    return crypto


def get_recent_trades(token_id: str) -> list[dict]:
    try:
        r = requests.get(
            "https://clob.polymarket.com/trades",
            params={"token_id": token_id, "limit": 20},
            timeout=8,
        )
        data = r.json()
        return data.get("data", []) if isinstance(data, dict) else []
    except:
        return []


def get_book(token_id: str) -> tuple:
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


def calc_lambda(trades: list) -> tuple:
    if len(trades) < 2:
        return 0.0, 0.0, ""
    try:
        latest = trades[0]
        prev   = trades[1]
        price_now  = float(latest.get("price", 0))
        price_prev = float(prev.get("price", 0))
        size_usdc  = float(latest.get("size", 0)) * price_now
        if size_usdc < MIN_ORDER_SIZE or price_prev == 0:
            return 0.0, 0.0, ""
        delta_p   = abs(price_now - price_prev)
        lam       = delta_p / size_usdc
        direction = "YES" if price_now > price_prev else "NO"
        return lam, size_usdc, direction
    except:
        return 0.0, 0.0, ""


def run():
    log.info("="*55)
    log.info("  🤖  Polymarket Kyle's Lambda Bot [DEBUG]")
    log.info("  Printing all markets to find correct ones")
    log.info("="*55)

    wallet     = PaperWallet()
    alerted    = set()
    last_trade = 0
    scan_count = 0

    while True:
        try:
            scan_count += 1
            log.info(f"\n🔍 Scan #{scan_count} — {datetime.now().strftime('%H:%M:%S')}")

            markets = get_markets()
            signals = 0

            for market in markets:
                q      = market.get("question", "")
                tokens = market.get("tokens", [])

                for tok in tokens:
                    tid   = tok.get("token_id", "")
                    label = tok.get("outcome", "")
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
                        log.info(f"     Token : {label} | Signal: {direction}")
                        log.info(f"     λ     : {lam:.8f} | Order: ${size:.0f}")

                        if time.time() - last_trade > 30:
                            wallet.bet(direction, q, BET_AMOUNT, lam, size)
                            last_trade = time.time()

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
