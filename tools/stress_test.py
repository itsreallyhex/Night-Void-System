"""Release-audit / stress suite for the database layer.

Standalone and dependency-free — no pytest, no Discord, no bot token. It drives
the real `database.Database` against a throwaway temp DB and asserts the
invariants that actually matter for a live economy: money is conserved under
concurrency, balances never go negative, atomic gates hold under a race, and the
newer subsystems (transfers, the live shop, leaver burns) behave.

    python3 tools/stress_test.py            # run everything
    echo $?                                 # 0 = all passed, 1 = a check failed

Run it before a deploy. It creates and deletes its own temp database, so it
never touches data/store.db.
"""

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running from repo root or tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database  # noqa: E402


# --------------------------------------------------------------------------- #
# Tiny test harness (no external deps)
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "✅" if ok else "❌"
        line = f"{mark} {name}"
        if not ok and detail:
            line += f"  -> {detail}"
        print(line)
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    async def raises(self, name: str, coro, exc=ValueError) -> None:
        try:
            await coro
            self.check(name, False, f"expected {exc.__name__}, nothing raised")
        except exc:
            self.check(name, True)
        except Exception as e:  # noqa: BLE001
            self.check(name, False, f"expected {exc.__name__}, got {type(e).__name__}: {e}")


def iso(dt: datetime) -> str:
    return dt.isoformat()


NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
async def s_schema(db: Database, r: Report) -> None:
    ov = await db.overview()
    tables = set(ov["tables"])
    expected = {
        "codes", "credits", "tickets", "reviews", "purchases", "redemptions",
        "transfers", "settings", "leavers", "users", "shop_items",
    }
    r.check("schema: all core tables created", expected <= tables,
            f"missing {expected - tables}")


async def s_balances(db: Database, r: Report) -> None:
    await db.add_credits(1, 500)
    r.check("balance: add + read", await db.get_balance(1) == 500)
    r.check("balance: deduct within funds", await db.deduct_credits(1, 200) is True)
    r.check("balance: value after deduct", await db.get_balance(1) == 300)
    r.check("balance: deduct over funds refused", await db.deduct_credits(1, 9999) is False)
    r.check("balance: unchanged after refused deduct", await db.get_balance(1) == 300)
    await r.raises("balance: deduct <= 0 raises", db.deduct_credits(1, 0))
    await r.raises("balance: add negative raises", db.add_credits(1, -5))
    new = await db.remove_credits(1, 10_000)
    r.check("balance: remove clamps at 0", new == 0)


async def s_transfer_conservation(db: Database, r: Report) -> None:
    sender, recipient = 100, 200
    await db.set_credits(sender, 20_000)
    await db.set_credits(recipient, 0)
    amount, fee = 1_000, 50  # sender has funds for exactly 20 of these

    async def one():
        return await db.transfer_credits(sender, recipient, amount, fee, iso(NOW))

    results = await asyncio.gather(*(one() for _ in range(30)))
    wins = sum(results)
    s_bal = await db.get_balance(sender)
    r_bal = await db.get_balance(recipient)
    burned = wins * fee
    r.check("transfer: exactly funded number succeed", wins == 20, f"wins={wins}")
    r.check("transfer: sender drained to 0", s_bal == 0, f"sender={s_bal}")
    r.check("transfer: recipient got amount-fee each", r_bal == wins * (amount - fee),
            f"recipient={r_bal}")
    r.check("transfer: money conserved (sender+recipient+burned)",
            s_bal + r_bal + burned == 20_000,
            f"{s_bal}+{r_bal}+{burned}")
    await r.raises("transfer: fee >= amount raises", db.transfer_credits(1, 2, 100, 100, iso(NOW)))


async def s_deduct_race(db: Database, r: Report) -> None:
    uid = 300
    await db.set_credits(uid, 10_000)
    cost = 1_000

    async def one():
        return await db.deduct_credits(uid, cost)

    results = await asyncio.gather(*(one() for _ in range(15)))
    wins = sum(results)
    bal = await db.get_balance(uid)
    r.check("deduct race: exactly balance//cost succeed", wins == 10, f"wins={wins}")
    r.check("deduct race: never negative, lands on 0", bal == 0, f"bal={bal}")


async def s_code_redemption(db: Database, r: Report) -> None:
    # Multi-use code consumed concurrently: never over-redeemed.
    await db.add_code("NIGHTVOID-MULTI0000000001", role_id=0, credits=100, max_uses=5)

    async def redeem(u):
        return await db.redeem_code("NIGHTVOID-MULTI0000000001", u, iso(NOW))

    results = await asyncio.gather(*(redeem(u) for u in range(12)))
    wins = sum(results)
    row = await db.get_code("NIGHTVOID-MULTI0000000001")
    r.check("code: multi-use consumed exactly max_uses", wins == 5, f"wins={wins}")
    r.check("code: uses == max_uses after race", row["uses"] == 5, f"uses={row['uses']}")
    r.check("code: flagged redeemed when exhausted", row["redeemed"] == 1)

    # Per-user redemption gate (log_redemption unique on (code,user)).
    async def logrow():
        return await db.log_redemption("NIGHTVOID-MULTI0000000001", 777, 0, 100, iso(NOW))

    ids = await asyncio.gather(*(logrow() for _ in range(5)))
    granted = [i for i in ids if i is not None]
    r.check("code: per-user gate lets exactly one redemption log", len(granted) == 1,
            f"granted={len(granted)}")


async def s_code_expiry(db: Database, r: Report) -> None:
    past = iso(NOW - timedelta(hours=1))
    future = iso(NOW + timedelta(hours=1))
    await db.add_code("NIGHTVOID-EXPIRED000000001", role_id=0, credits=1, expires_at=past)
    await db.add_code("NIGHTVOID-FUTURE0000000001", role_id=0, credits=1, expires_at=future)
    r.check("expiry: expired code refuses redemption",
            await db.redeem_code("NIGHTVOID-EXPIRED000000001", 1, iso(NOW)) is False)
    r.check("expiry: unexpired code redeems",
            await db.redeem_code("NIGHTVOID-FUTURE0000000001", 1, iso(NOW)) is True)


async def s_tickets(db: Database, r: Report) -> None:
    tid = await db.create_ticket(400, 4000, iso(NOW))
    r.check("ticket: first open created", tid is not None)
    r.check("ticket: one-open index blocks a second",
            await db.create_ticket(400, 4001, iso(NOW)) is None)
    r.check("tickreset: refuses while a ticket is open", await db.reset_tickets() is None)
    await db.close_ticket(tid, iso(NOW))
    deleted = await db.reset_tickets()
    r.check("tickreset: succeeds once nothing is open", deleted is not None and deleted >= 1,
            f"deleted={deleted}")


async def s_reviews(db: Database, r: Report) -> None:
    r.check("review: first for a ticket accepted",
            await db.add_review(500, 5, "great", iso(NOW), ticket_id=42) is True)
    r.check("review: second for same ticket rejected",
            await db.add_review(500, 1, "dupe", iso(NOW), ticket_id=42) is False)


async def s_shop(db: Database, r: Report) -> None:
    seed = [
        {"role_id": 111, "name": "Alpha", "cost": 1000, "description": "a"},
        {"role_id": 222, "name": "Beta", "cost": 2000, "description": "b"},
    ]
    n = await db.seed_shop_items(seed, iso(NOW))
    r.check("shop: seed inserts all items", n == 2, f"n={n}")
    r.check("shop: re-seed is a no-op (never clobbers live edits)",
            await db.seed_shop_items(seed, iso(NOW)) == 0)
    added = await db.upsert_shop_item(333, "Gamma", 3000, "c", iso(NOW))
    r.check("shop: upsert new returns True", added is True)
    edited = await db.upsert_shop_item(333, "Gamma", 3500, "c2", iso(NOW))
    r.check("shop: upsert existing returns False (edit)", edited is False)
    row = await db.get_shop_item(333)
    r.check("shop: edit updated cost", row["cost"] == 3500, dict(row))
    r.check("shop: list ordered cheapest-first",
            [i["role_id"] for i in await db.list_shop_items()] == [111, 222, 333])
    r.check("shop: remove existing True", await db.remove_shop_item(333) is True)
    r.check("shop: get after remove is None", await db.get_shop_item(333) is None)
    r.check("shop: remove missing False", await db.remove_shop_item(333) is False)


async def s_leavers(db: Database, r: Report) -> None:
    uid = 600
    await db.set_credits(uid, 5_000)
    await db.mark_leaver(uid, iso(NOW - timedelta(hours=25)))
    due = await db.due_leavers(iso(NOW - timedelta(hours=24)))
    r.check("leaver: past-grace member is due for burn", uid in due, f"due={due}")
    burned = await db.clear_balance(uid)
    r.check("leaver: clear_balance returns the burned amount", burned == 5_000, f"burned={burned}")
    r.check("leaver: balance gone after burn", await db.get_balance(uid) == 0)
    r.check("leaver: unmark returns True when pending", await db.unmark_leaver(uid) is True)
    r.check("leaver: unmark returns False when nothing pending",
            await db.unmark_leaver(uid) is False)


async def s_hostile_and_volume(db: Database, r: Report) -> None:
    # Hostile / edge string content stored + read back intact.
    weird = "'; DROP TABLE credits;-- 🌙\n\t\"weird\""
    await db.add_code("NIGHTVOID-WEIRD00000000001", role_id=0, credits=1)
    await db.add_purchase(900, "role", weird, 1, iso(NOW))
    got = (await db.get_purchases(900))[0]["item_name"]
    r.check("hostile: injection-y string round-trips verbatim", got == weird)
    r.check("hostile: credits table still present (no injection)",
            await db.get_balance(1) is not None)
    # Big amounts (1b) are fine.
    await db.set_credits(901, 1_000_000_000)
    r.check("volume: billion-credit balance stored", await db.get_balance(901) == 1_000_000_000)

    # Bulk insert 3,000 codes; count reflects it.
    before, _ = await db.count_codes()
    for i in range(3_000):
        await db.add_code(f"NIGHTVOID-BULK{i:011d}", role_id=0, credits=1)
    total, _ = await db.count_codes()
    r.check("volume: 3,000 codes inserted", total - before == 3_000, f"delta={total - before}")

    # Many balances -> circulating holder count is right.
    for u in range(2_000, 7_000):
        await db.add_credits(u, 10)
    circ = await db.circulating()
    r.check("volume: circulating counts all positive holders", circ["holders"] >= 5_000,
            f"holders={circ['holders']}")


async def s_backup(db: Database, r: Report, tmp: Path) -> None:
    dest = tmp / "snapshot.db"
    await db.backup_to(str(dest))
    r.check("backup: VACUUM INTO produced a file", dest.exists() and dest.stat().st_size > 0)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="nvstore-stress-"))
    db = Database(str(tmp / "store.db"))
    r = Report()
    try:
        await db.connect()
        await s_schema(db, r)
        await s_balances(db, r)
        await s_transfer_conservation(db, r)
        await s_deduct_race(db, r)
        await s_code_redemption(db, r)
        await s_code_expiry(db, r)
        await s_tickets(db, r)
        await s_reviews(db, r)
        await s_shop(db, r)
        await s_leavers(db, r)
        await s_hostile_and_volume(db, r)
        await s_backup(db, r, tmp)
    finally:
        await db.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 48)
    print(f"  {r.passed} passed, {r.failed} failed")
    print("=" * 48)
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
