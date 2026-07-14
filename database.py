"""Async SQLite layer (aiosqlite).

Owns the single shared connection, creates the schema on startup, and exposes
small atomic helpers used by the cogs. All credit mutations are performed with
conditional UPDATE statements so balances can never go negative under races.
"""

import asyncio
import logging
import os

import aiosqlite

log = logging.getLogger("nightvoid.database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS codes (
    code         TEXT    PRIMARY KEY,
    role_id      INTEGER NOT NULL,          -- 0 = no role (credit-only code)
    credits      INTEGER NOT NULL DEFAULT 0, -- >0 = grants this many credits
    max_uses     INTEGER NOT NULL DEFAULT 1, -- >1 = multi-use (giveaway) code
    uses         INTEGER NOT NULL DEFAULT 0,
    expires_at   TEXT,                       -- NULL = never expires
    redeemed     INTEGER NOT NULL DEFAULT 0, -- 1 = exhausted (uses >= max_uses)
    redeemed_by  INTEGER,                    -- last redeemer
    redeemed_at  TEXT                        -- last redemption time
);

CREATE TABLE IF NOT EXISTS credits (
    user_id  INTEGER PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'open',
    type        TEXT    NOT NULL DEFAULT 'store',  -- section: store | ask | suggest
    created_at  TEXT    NOT NULL,
    closed_at   TEXT
);

-- One-hold enforcement at the DB level: a user can hold at most one row whose
-- status is 'open'. A second insert raises sqlite3.IntegrityError.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_ticket
    ON tickets(user_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    text        TEXT,
    created_at  TEXT    NOT NULL,
    ticket_id   INTEGER NOT NULL DEFAULT 0  -- ticket that triggered the review request
);
-- One review per ticket, enforced at the DB level (the unique index in
-- _migrate covers ticket_id > 0; 0 marks legacy rows with no ticket link).

CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    item_type   TEXT    NOT NULL,   -- 'role' | 'service'
    item_name   TEXT    NOT NULL,
    cost        INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id);

-- Permanent audit log of every code redemption. Kept forever (the `codes` table
-- is auto-pruned, this is not) so redemption history stays fully searchable.
CREATE TABLE IF NOT EXISTS redemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code         TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    role_id      INTEGER NOT NULL DEFAULT 0,
    credits      INTEGER NOT NULL DEFAULT 0,
    redeemed_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_redemptions_user ON redemptions(user_id);
CREATE INDEX IF NOT EXISTS idx_redemptions_time ON redemptions(redeemed_at);

-- User-to-user credit transfers (/pay). `amount` is what the sender paid; the
-- recipient received amount - fee (the fee is burned). Also drives the rolling
-- 24h per-sender cap.
CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id     INTEGER NOT NULL,
    recipient_id  INTEGER NOT NULL,
    amount        INTEGER NOT NULL,
    fee           INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfers_sender_time
    ON transfers(sender_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Members who left and are inside the 24h grace period before their balance
-- is burned. Rejoining deletes the row; the hourly sweep in
-- cogs/extras/leavers.py burns whoever is still gone past the cutoff.
CREATE TABLE IF NOT EXISTS leavers (
    user_id  INTEGER PRIMARY KEY,
    left_at  TEXT    NOT NULL
);

-- Last known Discord username per user id, so the raw database is readable
-- by humans. Kept fresh by cogs/extras/usernames.py (startup backfill +
-- member join + throttled message activity). Display-only: bot logic always
-- keys on user_id.
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- Role-shop catalogue. Managed live by the owner (/shop-add, /shop-remove) and
-- read by cogs/shop.py. Seeded once from shop_config.json on first boot (see
-- Shop.cog_load); after that the DATABASE is the source of truth and editing
-- the JSON no longer changes anything. `sort` sets display order (lower first).
CREATE TABLE IF NOT EXISTS shop_items (
    role_id      INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    cost         INTEGER NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    sort         INTEGER NOT NULL DEFAULT 0,
    added_at     TEXT    NOT NULL
);

-- Honeypot channel trap. Single-guild bot, so exactly one config row and no
-- guild_id anywhere: CHECK (id = 1) makes a second row impossible at the DB
-- level (every insert must target id 1 and collide with the primary key).
-- Privacy by design: trigger_count is the ONLY record — no incident rows,
-- no timestamps, no user ids.
CREATE TABLE IF NOT EXISTS honeypot_config (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    channel_id        INTEGER NOT NULL,
    action_type       TEXT    NOT NULL
        CHECK (action_type IN ('kick', 'ban', 'remove_roles', 'alert_only')),
    embed_message_id  INTEGER,
    trigger_count     INTEGER NOT NULL DEFAULT 0,
    enabled           INTEGER NOT NULL DEFAULT 1
);

-- Roles fully exempt from the honeypot (no deletion, no action, no counter).
CREATE TABLE IF NOT EXISTS honeypot_safe_roles (
    role_id  INTEGER PRIMARY KEY
);

-- Human-readable views for DB Browser / sqlite3: same data as the base
-- tables but with usernames instead of raw ids. Dropped and recreated on
-- every startup so definition changes ship automatically.
DROP VIEW IF EXISTS v_balances;
CREATE VIEW v_balances AS
    SELECT COALESCE(u.username, '#' || c.user_id) AS member, c.balance
    FROM credits c LEFT JOIN users u ON u.user_id = c.user_id
    ORDER BY c.balance DESC;

DROP VIEW IF EXISTS v_purchases;
CREATE VIEW v_purchases AS
    SELECT p.id, COALESCE(u.username, '#' || p.user_id) AS member,
           p.item_type, p.item_name, p.cost, p.created_at
    FROM purchases p LEFT JOIN users u ON u.user_id = p.user_id
    ORDER BY p.id DESC;

DROP VIEW IF EXISTS v_reviews;
CREATE VIEW v_reviews AS
    SELECT r.id, COALESCE(u.username, '#' || r.user_id) AS member,
           r.stars, r.text, r.ticket_id, r.created_at
    FROM reviews r LEFT JOIN users u ON u.user_id = r.user_id
    ORDER BY r.id DESC;

DROP VIEW IF EXISTS v_tickets;
CREATE VIEW v_tickets AS
    SELECT t.id, COALESCE(u.username, '#' || t.user_id) AS member,
           t.status, t.type, t.created_at, t.closed_at
    FROM tickets t LEFT JOIN users u ON u.user_id = t.user_id
    ORDER BY t.id DESC;

DROP VIEW IF EXISTS v_transfers;
CREATE VIEW v_transfers AS
    SELECT tr.id, COALESCE(us.username, '#' || tr.sender_id) AS sender,
           COALESCE(ur.username, '#' || tr.recipient_id) AS recipient,
           tr.amount, tr.fee, tr.created_at
    FROM transfers tr
    LEFT JOIN users us ON us.user_id = tr.sender_id
    LEFT JOIN users ur ON ur.user_id = tr.recipient_id
    ORDER BY tr.id DESC;

DROP VIEW IF EXISTS v_redemptions;
CREATE VIEW v_redemptions AS
    SELECT r.id, COALESCE(u.username, '#' || r.user_id) AS member,
           r.code, r.credits, r.redeemed_at
    FROM redemptions r LEFT JOIN users u ON u.user_id = r.user_id
    ORDER BY r.id DESC;
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # Every coroutine shares one connection, so multi-statement
        # transactions must not interleave with other writes.
        self._tx_lock = asyncio.Lock()

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        # Durability across container kills / host migration (Railway redeploys
        # SIGTERM the process without a graceful shutdown). With WAL's default
        # synchronous=NORMAL the WAL isn't fsync'd per commit, so recent writes
        # can be lost when the volume re-attaches on a new host. FULL fsyncs the
        # WAL on every commit — safe for this low write volume.
        await self.conn.execute("PRAGMA synchronous=FULL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        """Additive migrations for databases created before a column existed.

        CREATE TABLE IF NOT EXISTS never alters an existing table, so new columns
        must be added by hand. Each step is a no-op once the column is present.
        """
        async with self.conn.execute("PRAGMA table_info(codes)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "credits" not in cols:
            await self.conn.execute(
                "ALTER TABLE codes ADD COLUMN credits INTEGER NOT NULL DEFAULT 0"
            )
        if "max_uses" not in cols:
            await self.conn.execute(
                "ALTER TABLE codes ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1"
            )
        if "uses" not in cols:
            await self.conn.execute(
                "ALTER TABLE codes ADD COLUMN uses INTEGER NOT NULL DEFAULT 0"
            )
            # Backfill: pre-migration redeemed codes must read as exhausted
            # under the new uses/max_uses accounting.
            await self.conn.execute(
                "UPDATE codes SET uses = max_uses WHERE redeemed = 1"
            )
        if "expires_at" not in cols:
            await self.conn.execute("ALTER TABLE codes ADD COLUMN expires_at TEXT")

        async with self.conn.execute("PRAGMA table_info(tickets)") as cur:
            ticket_cols = {row["name"] for row in await cur.fetchall()}
        if "type" not in ticket_cols:
            await self.conn.execute(
                "ALTER TABLE tickets ADD COLUMN type TEXT NOT NULL DEFAULT 'store'"
            )

        async with self.conn.execute("PRAGMA table_info(reviews)") as cur:
            review_cols = {row["name"] for row in await cur.fetchall()}
        if "ticket_id" not in review_cols:
            await self.conn.execute(
                "ALTER TABLE reviews ADD COLUMN ticket_id INTEGER NOT NULL DEFAULT 0"
            )
        # Lives here (not in SCHEMA) because it must run after the column
        # exists on pre-migration databases.
        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_review_per_ticket "
            "ON reviews(ticket_id) WHERE ticket_id > 0"
        )

        # Atomic per-user gate for multi-use codes: without it, one user
        # double-submitting fast enough could consume two uses of a giveaway
        # code. Falls back gracefully if legacy data already has duplicates.
        try:
            await self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_redemption_per_user "
                "ON redemptions(code, user_id)"
            )
        except aiosqlite.IntegrityError:
            log.warning(
                "redemptions has duplicate (code, user) rows; per-user "
                "redemption gate index not created."
            )

    async def close(self) -> None:
        if self.conn is not None:
            # Fold the WAL back into the main db file before closing so a clean
            # shutdown leaves nothing pending in the WAL.
            try:
                await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            await self.conn.close()
            self.conn = None

    async def mark_leaver(self, user_id: int, left_at: str) -> None:
        """Start (or restart) the grace-period clock for a member who left."""
        await self.conn.execute(
            "INSERT INTO leavers (user_id, left_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET left_at = excluded.left_at",
            (user_id, left_at),
        )
        await self.conn.commit()

    async def unmark_leaver(self, user_id: int) -> bool:
        """Cancel a pending burn (member rejoined). True if one was pending."""
        cur = await self.conn.execute(
            "DELETE FROM leavers WHERE user_id = ?", (user_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def due_leavers(self, cutoff: str) -> list[int]:
        """User ids whose grace period expired (left_at at or before cutoff)."""
        async with self.conn.execute(
            "SELECT user_id FROM leavers WHERE left_at <= ?", (cutoff,)
        ) as cur:
            return [row["user_id"] for row in await cur.fetchall()]

    async def clear_balance(self, user_id: int) -> int:
        """Delete a user's live balance row (leave cleanup), returning the
        amount that was burned. History tables (purchases, redemptions,
        reviews, transfers) and the username record are kept for auditing."""
        async with self._tx_lock:
            async with self.conn.execute(
                "SELECT balance FROM credits WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return 0
            await self.conn.execute("DELETE FROM credits WHERE user_id = ?", (user_id,))
            await self.conn.commit()
            return row["balance"]

    async def upsert_users(self, rows: list[tuple[int, str]], updated_at: str) -> None:
        """Remember (or refresh) the username for each (user_id, username) pair.
        Display-only data for the human-readable v_* views."""
        if not rows:
            return
        async with self._tx_lock:
            await self.conn.executemany(
                """
                INSERT INTO users (user_id, username, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username, updated_at = excluded.updated_at
                """,
                [(uid, name, updated_at) for uid, name in rows],
            )
            await self.conn.commit()

    async def backup_to(self, path: str) -> None:
        """Write a consistent snapshot of the whole database to `path`.

        VACUUM INTO is atomic, includes everything still sitting in the WAL,
        and compacts free pages — safe to run while the bot is live. The
        target file must not already exist (SQLite refuses to overwrite).
        """
        async with self._tx_lock:
            await self.conn.execute("VACUUM INTO ?", (path,))

    # ----------------------------------------------------------------- #
    # Credits
    # ----------------------------------------------------------------- #
    async def get_balance(self, user_id: int) -> int:
        async with self.conn.execute(
            "SELECT balance FROM credits WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["balance"] if row else 0

    async def top_balances(
        self, limit: int = 10, exclude: list[int] | None = None
    ) -> list[aiosqlite.Row]:
        """Return the highest balances (descending), skipping `exclude` ids and
        anyone at zero. Ties break by user_id so the ordering is stable."""
        exclude = exclude or []
        query = "SELECT user_id, balance FROM credits WHERE balance > 0"
        params: list = []
        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            query += f" AND user_id NOT IN ({placeholders})"
            params.extend(exclude)
        query += " ORDER BY balance DESC, user_id ASC LIMIT ?"
        params.append(limit)
        async with self.conn.execute(query, params) as cur:
            return await cur.fetchall()

    async def add_credits(self, user_id: int, amount: int) -> None:
        """Atomic upsert add. Used by message/voice earning.

        Defense-in-depth: refuses a negative amount so a future caller can't
        silently drive a balance below zero (use deduct_credits/remove_credits
        for subtraction). A zero amount is a harmless no-op.
        """
        if amount < 0:
            raise ValueError(f"add_credits amount must be non-negative, got {amount}")
        if amount == 0:
            return
        await self.conn.execute(
            """
            INSERT INTO credits (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
            """,
            (user_id, amount),
        )
        await self.conn.commit()

    async def deduct_credits(self, user_id: int, amount: int) -> bool:
        """Atomically deduct `amount`. Returns False if balance insufficient.

        Defense-in-depth: refuses a non-positive amount — a negative `amount`
        would otherwise *add* credits (balance - (-x)) and always pass the
        balance check, turning a misconfigured cost into a free-money faucet.
        """
        if amount <= 0:
            raise ValueError(f"deduct_credits amount must be positive, got {amount}")
        cur = await self.conn.execute(
            "UPDATE credits SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def transfer_credits(
        self, sender_id: int, recipient_id: int, amount: int, fee: int, created_at: str
    ) -> bool:
        """Atomically move credits between users (/pay).

        The sender pays `amount`; the recipient receives `amount - fee` (the
        fee is burned). Deduct, credit, and the audit row commit together —
        returns False (nothing changes) if the sender's balance is short.
        """
        if amount <= 0:
            raise ValueError(f"transfer amount must be positive, got {amount}")
        if not (0 <= fee < amount):
            raise ValueError(f"fee must be within [0, amount), got {fee}")
        async with self._tx_lock:
            cur = await self.conn.execute(
                "UPDATE credits SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (amount, sender_id, amount),
            )
            if cur.rowcount == 0:
                # The conditional UPDATE touched nothing, so there is nothing
                # to undo — and a rollback here would discard other
                # coroutines' uncommitted writes on the shared connection.
                return False
            await self.conn.execute(
                """
                INSERT INTO credits (user_id, balance) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance
                """,
                (recipient_id, amount - fee),
            )
            await self.conn.execute(
                "INSERT INTO transfers (sender_id, recipient_id, amount, fee, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sender_id, recipient_id, amount, fee, created_at),
            )
            await self.conn.commit()
        return True

    async def transfers_sent_since(self, sender_id: int, since: str) -> int:
        """Total amount this user sent via /pay at or after `since` (ISO UTC).
        Drives the rolling daily cap."""
        async with self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM transfers "
            "WHERE sender_id = ? AND created_at >= ?",
            (sender_id, since),
        ) as cur:
            return (await cur.fetchone())["total"]

    async def set_credits(self, user_id: int, amount: int) -> None:
        """Owner override: set an exact balance (upsert)."""
        await self.conn.execute(
            """
            INSERT INTO credits (user_id, balance) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance
            """,
            (user_id, amount),
        )
        await self.conn.commit()

    async def remove_credits(self, user_id: int, amount: int) -> int:
        """Owner override: subtract `amount`, clamped at 0. Returns new balance."""
        await self.conn.execute(
            """
            INSERT INTO credits (user_id, balance) VALUES (?, 0)
            ON CONFLICT(user_id) DO UPDATE
                SET balance = MAX(balance - ?, 0)
            """,
            (user_id, amount),
        )
        await self.conn.commit()
        return await self.get_balance(user_id)

    # ----------------------------------------------------------------- #
    # Tickets
    # ----------------------------------------------------------------- #
    async def get_open_ticket(self, user_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE user_id = ? AND status = 'open'", (user_id,)
        ) as cur:
            return await cur.fetchone()

    async def create_ticket(
        self, user_id: int, channel_id: int, created_at: str, ticket_type: str = "store"
    ) -> int | None:
        """Insert an open ticket. Returns row id, or None if the unique index
        rejected it (the user already has an open ticket)."""
        try:
            cur = await self.conn.execute(
                "INSERT INTO tickets (user_id, channel_id, status, created_at, type) "
                "VALUES (?, ?, 'open', ?, ?)",
                (user_id, channel_id, created_at, ticket_type),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def reset_tickets(self) -> int | None:
        """Owner maintenance: wipe ticket history and restart numbering at #1.

        Refuses (returns None) while any ticket is open — those numbers are
        live in channel names. Existing reviews are detached from their old
        ticket ids (ticket_id=0, the legacy marker) so the one-review-per-ticket
        unique index can't collide when numbers get reused. Returns the number
        of deleted ticket rows.
        """
        async with self._tx_lock:
            async with self.conn.execute(
                "SELECT COUNT(*) AS n FROM tickets WHERE status = 'open'"
            ) as cur:
                if (await cur.fetchone())["n"]:
                    return None
            cur = await self.conn.execute("DELETE FROM tickets")
            deleted = cur.rowcount
            await self.conn.execute("UPDATE reviews SET ticket_id = 0 WHERE ticket_id > 0")
            try:
                await self.conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tickets'")
            except aiosqlite.OperationalError:
                pass  # sqlite_sequence doesn't exist yet (no AUTOINCREMENT insert ever)
            await self.conn.commit()
        return deleted

    async def get_open_tickets(self) -> list[aiosqlite.Row]:
        """Every currently-open ticket (for the inactivity auto-close sweep)."""
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE status = 'open'"
        ) as cur:
            return await cur.fetchall()

    async def get_ticket_by_channel(self, channel_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,)
        ) as cur:
            return await cur.fetchone()

    async def close_ticket(self, ticket_id: int, closed_at: str) -> None:
        await self.conn.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ? WHERE id = ?",
            (closed_at, ticket_id),
        )
        await self.conn.commit()

    # ----------------------------------------------------------------- #
    # Codes
    # ----------------------------------------------------------------- #
    async def get_code(self, code: str) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT * FROM codes WHERE code = ?", (code,)
        ) as cur:
            return await cur.fetchone()

    async def add_code(
        self,
        code: str,
        role_id: int,
        credits: int = 0,
        max_uses: int = 1,
        expires_at: str | None = None,
    ) -> bool:
        """Insert a new unredeemed code. Returns False if the code already exists.

        `role_id=0` means the code grants no role; `credits>0` means it grants
        that many credits. A code may grant a role, credits, or both.
        `max_uses>1` makes it a multi-use (giveaway) code; `expires_at` is an
        ISO UTC timestamp after which it can no longer be redeemed (None = never).
        """
        try:
            await self.conn.execute(
                "INSERT INTO codes (code, role_id, credits, max_uses, uses, expires_at, redeemed) "
                "VALUES (?, ?, ?, ?, 0, ?, 0)",
                (code, role_id, credits, max_uses, expires_at),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def list_codes(
        self, redeemed: bool | None = None, role_id: int | None = None
    ) -> list[aiosqlite.Row]:
        """Return code rows, newest-redeemed last, optionally filtered.

        `redeemed=None` returns every code; True/False narrows to used/unused.
        `role_id` narrows to a single linked role. Unredeemed codes sort first.
        """
        query = "SELECT * FROM codes"
        clauses: list[str] = []
        params: list = []
        if redeemed is not None:
            clauses.append("redeemed = ?")
            params.append(1 if redeemed else 0)
        if role_id is not None:
            clauses.append("role_id = ?")
            params.append(role_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY redeemed ASC, redeemed_at ASC, code ASC"
        async with self.conn.execute(query, params) as cur:
            return await cur.fetchall()

    async def count_codes(self) -> tuple[int, int]:
        """Return (total, redeemed) code counts."""
        async with self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(redeemed), 0) AS used FROM codes"
        ) as cur:
            row = await cur.fetchone()
        return row["total"], row["used"]

    async def delete_unused_codes(self, amount: int, role_id: int | None = None) -> int:
        """Delete up to `amount` unredeemed codes (optionally for one role).

        Redeemed codes are never touched (they record who redeemed what).
        Returns the number actually deleted.
        """
        if role_id is None:
            cur = await self.conn.execute(
                "DELETE FROM codes WHERE code IN "
                "(SELECT code FROM codes WHERE redeemed = 0 LIMIT ?)",
                (amount,),
            )
        else:
            cur = await self.conn.execute(
                "DELETE FROM codes WHERE code IN "
                "(SELECT code FROM codes WHERE redeemed = 0 AND role_id = ? LIMIT ?)",
                (role_id, amount),
            )
        await self.conn.commit()
        return cur.rowcount

    async def delete_old_redeemed_codes(self, before: str) -> int:
        """Delete redeemed codes whose redeemed_at is older than `before`.

        `before` is an ISO-8601 UTC timestamp; string comparison is safe because
        every redeemed_at is written in the same fixed format. Keeps the used-code
        list from growing without bound. Unused codes are never touched.
        """
        cur = await self.conn.execute(
            "DELETE FROM codes WHERE redeemed = 1 AND redeemed_at IS NOT NULL "
            "AND redeemed_at < ?",
            (before,),
        )
        await self.conn.commit()
        return cur.rowcount

    async def redeem_code(self, code: str, user_id: int, redeemed_at: str) -> bool:
        """Atomically consume one use of a code. Returns False if it was already
        exhausted or expired (lost the race).

        The `redeemed` flag flips to 1 on the final use so every existing
        redeemed-based query (listing, counting, pruning) keeps working for
        multi-use codes.
        """
        cur = await self.conn.execute(
            "UPDATE codes SET uses = uses + 1, "
            "redeemed = CASE WHEN uses + 1 >= max_uses THEN 1 ELSE 0 END, "
            "redeemed_by = ?, redeemed_at = ? "
            "WHERE code = ? AND uses < max_uses "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, redeemed_at, code, redeemed_at),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def has_redeemed(self, code: str, user_id: int) -> bool:
        """Whether this user already redeemed this code (via the permanent log).
        Used to limit multi-use codes to one redemption per user."""
        async with self.conn.execute(
            "SELECT 1 FROM redemptions WHERE code = ? AND user_id = ? LIMIT 1",
            (code, user_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def delete_expired_codes(self, before: str) -> int:
        """Delete never-exhausted codes whose expires_at passed before `before`.

        Exhausted codes are handled by delete_old_redeemed_codes; redemption
        history survives in the permanent redemptions log either way.
        """
        cur = await self.conn.execute(
            "DELETE FROM codes WHERE redeemed = 0 AND expires_at IS NOT NULL "
            "AND expires_at < ?",
            (before,),
        )
        await self.conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------------- #
    # Redemption log (permanent, searchable — never pruned)
    # ----------------------------------------------------------------- #
    async def log_redemption(
        self, code: str, user_id: int, role_id: int, credits: int, redeemed_at: str
    ) -> int | None:
        """Record a redemption. Returns the row id, or None if this (code, user)
        pair is already logged — the atomic once-per-user gate for multi-use
        codes (INSERT OR IGNORE against the unique index)."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO redemptions (code, user_id, role_id, credits, redeemed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, user_id, role_id, credits, redeemed_at),
        )
        await self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    async def remove_redemption(self, row_id: int) -> None:
        """Compensation: drop a reserved log row when the redemption it gated
        ultimately failed (code exhausted/expired in the race window)."""
        await self.conn.execute("DELETE FROM redemptions WHERE id = ?", (row_id,))
        await self.conn.commit()

    @staticmethod
    def _redemption_where(
        user_id: int | None, since: str | None, until: str | None
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if since:
            clauses.append("redeemed_at >= ?")
            params.append(since)
        if until:
            clauses.append("redeemed_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    async def count_redemptions(
        self, user_id: int | None = None, since: str | None = None, until: str | None = None
    ) -> int:
        where, params = self._redemption_where(user_id, since, until)
        async with self.conn.execute(
            f"SELECT COUNT(*) AS n FROM redemptions{where}", params
        ) as cur:
            return (await cur.fetchone())["n"]

    async def search_redemptions(
        self,
        user_id: int | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[aiosqlite.Row]:
        """Newest-first page of the redemption log, filtered by user and/or date."""
        where, params = self._redemption_where(user_id, since, until)
        async with self.conn.execute(
            f"SELECT * FROM redemptions{where} "
            f"ORDER BY redeemed_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ) as cur:
            return await cur.fetchall()

    # ----------------------------------------------------------------- #
    # Reviews
    # ----------------------------------------------------------------- #
    async def add_review(
        self, user_id: int, stars: int, text: str | None, created_at: str, ticket_id: int = 0
    ) -> bool:
        """Insert a review. Returns False if the ticket already has one (the
        partial unique index rejected it) — the atomic once-per-ticket guard."""
        try:
            await self.conn.execute(
                "INSERT INTO reviews (user_id, stars, text, created_at, ticket_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, stars, text, created_at, ticket_id),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def ticket_reviewed(self, ticket_id: int) -> bool:
        """Whether a review was already submitted for this ticket."""
        if ticket_id <= 0:
            return False
        async with self.conn.execute(
            "SELECT 1 FROM reviews WHERE ticket_id = ? LIMIT 1", (ticket_id,)
        ) as cur:
            return await cur.fetchone() is not None

    # ----------------------------------------------------------------- #
    # Purchases
    # ----------------------------------------------------------------- #
    async def add_purchase(
        self, user_id: int, item_type: str, item_name: str, cost: int, created_at: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO purchases (user_id, item_type, item_name, cost, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, item_type, item_name, cost, created_at),
        )
        await self.conn.commit()

    async def get_purchases(self, user_id: int, limit: int = 25) -> list[aiosqlite.Row]:
        async with self.conn.execute(
            "SELECT * FROM purchases WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            return await cur.fetchall()

    # ----------------------------------------------------------------- #
    # Role shop catalogue (managed live by the owner)
    # ----------------------------------------------------------------- #
    async def seed_shop_items(self, items: list[dict], added_at: str) -> int:
        """First-boot seed from shop_config.json. No-op (returns 0) once the
        table already holds anything, so it never clobbers live edits. Each item
        is {role_id, name, cost, description}. Returns the number inserted."""
        if not items:
            return 0
        async with self._tx_lock:
            async with self.conn.execute("SELECT 1 FROM shop_items LIMIT 1") as cur:
                if await cur.fetchone() is not None:
                    return 0
            await self.conn.executemany(
                "INSERT OR IGNORE INTO shop_items "
                "(role_id, name, cost, description, sort, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (it["role_id"], it["name"], it["cost"],
                     it.get("description", ""), i, added_at)
                    for i, it in enumerate(items)
                ],
            )
            await self.conn.commit()
        return len(items)

    async def list_shop_items(self) -> list[aiosqlite.Row]:
        """The catalogue in display order (sort, then cheapest, then id)."""
        async with self.conn.execute(
            "SELECT role_id, name, cost, description, sort FROM shop_items "
            "ORDER BY sort ASC, cost ASC, role_id ASC"
        ) as cur:
            return await cur.fetchall()

    async def get_shop_item(self, role_id: int) -> aiosqlite.Row | None:
        async with self.conn.execute(
            "SELECT role_id, name, cost, description, sort FROM shop_items "
            "WHERE role_id = ?",
            (role_id,),
        ) as cur:
            return await cur.fetchone()

    async def upsert_shop_item(
        self, role_id: int, name: str, cost: int, description: str, added_at: str
    ) -> bool:
        """Add a shop item, or update name/cost/description if the role is
        already listed. New items sort to the end; an update keeps the existing
        position. Returns True if newly added, False if it updated an existing."""
        newly_added = await self.get_shop_item(role_id) is None
        await self.conn.execute(
            """
            INSERT INTO shop_items (role_id, name, cost, description, sort, added_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT MAX(sort) + 1 FROM shop_items), 0), ?)
            ON CONFLICT(role_id) DO UPDATE SET
                name = excluded.name, cost = excluded.cost,
                description = excluded.description
            """,
            (role_id, name, cost, description, added_at),
        )
        await self.conn.commit()
        return newly_added

    async def remove_shop_item(self, role_id: int) -> bool:
        """Delete a shop item. Returns True if a row was actually removed."""
        cur = await self.conn.execute(
            "DELETE FROM shop_items WHERE role_id = ?", (role_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ----------------------------------------------------------------- #
    # Honeypot (single config row + safe-role list)
    # ----------------------------------------------------------------- #
    async def get_honeypot_config(self) -> aiosqlite.Row | None:
        """The single honeypot config row, or None before first setup."""
        async with self.conn.execute(
            "SELECT * FROM honeypot_config WHERE id = 1"
        ) as cur:
            return await cur.fetchone()

    async def upsert_honeypot_config(
        self, channel_id: int, action_type: str, embed_message_id: int
    ) -> None:
        """Create or repoint the single config row (id is hard-pinned to 1).

        A re-setup rearms the trap (enabled = 1) but the lifetime
        trigger_count survives — it's the feature's only record."""
        if action_type not in ("kick", "ban", "remove_roles", "alert_only"):
            raise ValueError(f"invalid honeypot action_type: {action_type!r}")
        await self.conn.execute(
            """
            INSERT INTO honeypot_config (id, channel_id, action_type, embed_message_id)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                channel_id = excluded.channel_id,
                action_type = excluded.action_type,
                embed_message_id = excluded.embed_message_id,
                enabled = 1
            """,
            (channel_id, action_type, embed_message_id),
        )
        await self.conn.commit()

    async def set_honeypot_embed_message_id(self, message_id: int) -> None:
        """Repoint the tracking embed (self-heal after the original was lost)."""
        await self.conn.execute(
            "UPDATE honeypot_config SET embed_message_id = ? WHERE id = 1",
            (message_id,),
        )
        await self.conn.commit()

    async def set_honeypot_enabled(self, enabled: bool) -> None:
        """Arm or disarm the trap without touching the rest of the config."""
        await self.conn.execute(
            "UPDATE honeypot_config SET enabled = ? WHERE id = 1",
            (1 if enabled else 0,),
        )
        await self.conn.commit()

    async def set_honeypot_action(self, action_type: str) -> None:
        """Change the configured action. Raises ValueError on an unknown type
        (defense-in-depth alongside the CHECK constraint)."""
        if action_type not in ("kick", "ban", "remove_roles", "alert_only"):
            raise ValueError(f"invalid honeypot action_type: {action_type!r}")
        await self.conn.execute(
            "UPDATE honeypot_config SET action_type = ? WHERE id = 1",
            (action_type,),
        )
        await self.conn.commit()

    async def increment_honeypot_counter(self) -> int:
        """Atomically bump the trigger counter and return the new total.

        UPDATE + SELECT run under the tx lock so two concurrent triggers can't
        read the same value. Returns 0 if setup never ran (no row to bump)."""
        async with self._tx_lock:
            await self.conn.execute(
                "UPDATE honeypot_config SET trigger_count = trigger_count + 1 "
                "WHERE id = 1"
            )
            async with self.conn.execute(
                "SELECT trigger_count FROM honeypot_config WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            await self.conn.commit()
        return row["trigger_count"] if row else 0

    async def add_safe_role(self, role_id: int) -> None:
        """Exempt a role from the honeypot. Adding twice is a no-op."""
        await self.conn.execute(
            "INSERT OR IGNORE INTO honeypot_safe_roles (role_id) VALUES (?)",
            (role_id,),
        )
        await self.conn.commit()

    async def remove_safe_role(self, role_id: int) -> bool:
        """Un-exempt a role. Returns True if a row was actually removed."""
        cur = await self.conn.execute(
            "DELETE FROM honeypot_safe_roles WHERE role_id = ?", (role_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_safe_roles(self) -> list[int]:
        """Every exempt role id (stable order for display)."""
        async with self.conn.execute(
            "SELECT role_id FROM honeypot_safe_roles ORDER BY role_id ASC"
        ) as cur:
            return [row["role_id"] for row in await cur.fetchall()]

    # ----------------------------------------------------------------- #
    # Economy (for the /economy admin dashboard)
    # ----------------------------------------------------------------- #
    async def circulating(self, exclude: int | None = None) -> dict:
        """Current in-wallet totals: sum of all positive balances + holder count.

        `exclude` drops one user_id (the owner, who can self-grant credits) so
        it doesn't skew the circulating supply.
        """
        query = "SELECT COUNT(*) AS holders, COALESCE(SUM(balance), 0) AS total " \
                "FROM credits WHERE balance > 0"
        params: list = []
        if exclude is not None:
            query += " AND user_id != ?"
            params.append(exclude)
        async with self.conn.execute(query, params) as cur:
            row = await cur.fetchone()
        return {"holders": row["holders"], "total": row["total"]}

    async def economy_window(
        self, since: str | None = None, exclude: int | None = None
    ) -> dict:
        """Economy activity within a time window.

        `since` is an ISO-8601 UTC timestamp (as produced by
        datetime.now(timezone.utc).isoformat()); rows with created_at >= since
        are counted. `since=None` covers all time. String comparison is safe
        here because every created_at is written in the same fixed UTC format.
        `exclude` drops one user_id (the owner) from every aggregate.

        Only the *sink* side (credits spent via purchases) has a history — credit
        earning is not journalled, so faucet-over-time is not available.
        """
        clauses: list[str] = []
        params: list = []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if exclude is not None:
            clauses.append("user_id != ?")
            params.append(exclude)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        spent = {"role": {"n": 0, "sum": 0}, "service": {"n": 0, "sum": 0}}
        async with self.conn.execute(
            f"SELECT item_type, COUNT(*) AS n, COALESCE(SUM(cost), 0) AS s "
            f"FROM purchases{where} GROUP BY item_type",
            params,
        ) as cur:
            for r in await cur.fetchall():
                if r["item_type"] in spent:
                    spent[r["item_type"]] = {"n": r["n"], "sum": r["s"]}

        async with self.conn.execute(
            f"SELECT COUNT(*) AS n FROM reviews{where}", params
        ) as cur:
            reviews = (await cur.fetchone())["n"]

        return {
            "spent_total": spent["role"]["sum"] + spent["service"]["sum"],
            "spent_count": spent["role"]["n"] + spent["service"]["n"],
            "role_spent": spent["role"]["sum"],
            "service_spent": spent["service"]["sum"],
            "reviews": reviews,
        }

    # ----------------------------------------------------------------- #
    # Settings (key/value)
    # ----------------------------------------------------------------- #
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await self.conn.commit()

    # ----------------------------------------------------------------- #
    # Diagnostics
    # ----------------------------------------------------------------- #
    async def overview(self, exclude: int | None = None) -> dict:
        """Aggregate a full snapshot of the database for the owner !db command.

        `exclude` drops one user_id (the owner, who can self-grant credits) from
        the credit / review / purchase aggregates so his own balance and test
        activity don't skew the numbers. The raw per-table row counts are left
        untouched — they report the true number of rows on disk.
        """
        data: dict = {}
        # Reusable `AND user_id != ?` fragment for the economy aggregates.
        excl_and = " AND user_id != ?" if exclude is not None else ""
        excl_where = " WHERE user_id != ?" if exclude is not None else ""
        excl_p: list = [exclude] if exclude is not None else []

        # Per-table row counts (table names come from the schema, never user input).
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ) as cur:
            tables = [r["name"] for r in await cur.fetchall()]
        counts: dict[str, int] = {}
        for t in tables:
            async with self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}") as cur:
                counts[t] = (await cur.fetchone())["n"]
        data["tables"] = counts

        total, used = await self.count_codes()
        data["codes"] = {"total": total, "used": used, "left": total - used}

        async with self.conn.execute(
            f"SELECT COUNT(*) AS holders, COALESCE(SUM(balance), 0) AS total "
            f"FROM credits WHERE balance > 0{excl_and}",
            excl_p,
        ) as cur:
            row = await cur.fetchone()
            data["credits"] = {"holders": row["holders"], "total": row["total"]}

        async with self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status"
        ) as cur:
            tstatus = {r["status"]: r["n"] for r in await cur.fetchall()}
        data["tickets"] = {"open": tstatus.get("open", 0), "closed": tstatus.get("closed", 0)}

        async with self.conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(AVG(stars), 0) AS avg "
            f"FROM reviews{excl_where}",
            excl_p,
        ) as cur:
            row = await cur.fetchone()
            data["reviews"] = {"count": row["n"], "avg": round(row["avg"], 2)}

        async with self.conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(cost), 0) AS spent "
            f"FROM purchases{excl_where}",
            excl_p,
        ) as cur:
            row = await cur.fetchone()
            data["purchases"] = {"count": row["n"], "spent": row["spent"]}

        async with self.conn.execute("SELECT key, value FROM settings") as cur:
            data["settings"] = {r["key"]: r["value"] for r in await cur.fetchall()}

        return data
