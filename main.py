"""
Polymarket Kyle's Lambda Bot
Only trades markets that are LIVE RIGHT NOW.
Watches for large orders that move price more than expected (high lambda).
"""

import time, logging, random, requests, sys
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

LAMBDA_THRESHOLD = 0.000003
MIN_ORDER_SIZE   = 50
BET_AMOUNT       = 5.0
SCAN_INTERVAL    = 20

CRYPTO_KEYWORDS = [
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp"
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
        log.info(f"       λ={lam:.8f} | order=${size:.0f} | {question[:55]}")

    def summary(self):
        total = self.wins + self.losses
        wr  = f"{self.wins/total:.0%}" if total else "n/a"
        roi = (self.balance - self.start) / self.start
        log.info(f"\n{'='*55}")
        log.info(f"  💰 Balance: ${self.balance:.2f} | ROI: {roi:+.1%}")
        log.info(f"  🎯 Win rate: {wr} ({self.wins}W/{self.losses}L/{total} trades)")
        log.info(f"{'='*55}\n")


def get_live_markets() -> list[dict]:
    """
    Fetch markets and keep only ones that are LIVE RIGHT NOW.
    A market is live if: startDate <= now <= endDate
    """
    all_markets = {}
    now = datetime.now(timezone.utc)

    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": 100},
            timeout=12,
        )
        for m in r.json():
            mid = m.get("conditionId", "")
            if mid:
                all_markets[mid] = m
    except Exception as e:
        log.error(f"  ❌ Fetch failed: {e}")
        return []

    markets = list(all_markets.values())
    live_crypto = []

    for m in markets:
        q = m.get("question", "").lower()

        # Must be a crypto market
        if not any(kw in q for kw in CRYPTO_KEYWORDS):
            continue

        # Must be open right now — check start/end times
        start_str = m.get("startDate") or m.get("startDateIso")
        end_str   = m.get("endDate")   or m.get("endDateIso")

        if start_str and end_str:
            try:
                # Parse ISO timestamps
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                end   = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                if not (start <= now <= end):
                    continue   # not open yet, or already closed
            except:
                pass  # if we can't parse dates, include it anyway

        live_crypto.append(m)

    log.info(f"  📡 {len(markets)} total | {len(live_crypto)} crypto live now")
    for m in live_crypto[:10]:
        log.info(f"     🟢 {m.get('question','')[:60]}")

    if not live_crypto:
        log.info("  ⏰ No live crypto markets right now — waiting for next window")

    return live_crypto


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
    """λ = ΔP / Q"""
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
    log.info("  🤖  Polymarket Kyle's Lambda Bot")
    log.info("  Strategy: Follow informed order flow")
    log.info("  Mode: PAPER — no real money")
    log.info(f"  λ threshold: {LAMBDA_THRESHOLD} | Min order: ${MIN_ORDER_SIZE}")
    log.info("="*55)

    wallet     = PaperWallet()
    alerted    = set()
    last_trade = 0
    scan_count = 0

    while True:
        try:
            scan_count += 1
            now_str = datetime.now().strftime('%H:%M:%S')
            log.info(f"\n🔍 Scan #{scan_count} — {now_str}")

            markets = get_live_markets()
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
                        log.info(f"\n  🚨 INFORMED FLOW DETECTED!")
                        log.info(f"     {q[:55]}")
                        log.info(f"     Token : {label} | Signal: {direction}")
                        log.info(f"     λ     : {lam:.8f}")
                        log.info(f"     Order : ${size:.0f} USDC | Price: ask={ask}")

                        if time.time() - last_trade > 30:
                            wallet.bet(direction, q, BET_AMOUNT, lam, size)
                            last_trade = time.time()
                        else:
                            log.info(f"     ⏳ Cooldown — skipping")

            if signals == 0 and markets:
                log.info("  — Markets live but no informed flow yet")

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
