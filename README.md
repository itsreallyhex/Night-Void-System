# Night Void System — Discord Bot

A single-guild Discord bot for the [Night Void Server](https://discord.gg/788VYYWNDF) community. It handles
support tickets, a credit economy (earned by chatting or sitting in voice),
redeemable codes, a role shop, paid minor services, reviews,
announcements, a join-verification gate, and a honeypot channel for
compromised-account detection. User-facing text is Saudi Arabic; the code and
docs are English.

Built on [discord.py](https://discordpy.readthedocs.io/) 2.4+ with async
SQLite (`aiosqlite`) — one cog per system, nothing fancier than that.

---

## Features at a glance

| # | System | What it does |
|---|--------|--------------|
| 1 | **Tickets** | One open ticket per user (enforced by a DB unique index). Private channel or thread; closing it triggers a review. Staff get an in-ticket admin menu (copy transcript, remind opener, close with a stated reason). |
| 2 | **Codes** | Redeemable `NIGHTVOID-…` codes that grant a **role, credits, or both**. Redeem via a persistent panel + modal. Atomic against double-spend. |
| 3 | **Reviews** | On ticket close, DMs the customer a 1–5★ rating + modal; accepted reviews post to a reviews channel. Admin toggle, state persisted. |
| 4 | **Announcements** | Admin `/announce` posts a rich embed with optional image and `@here`/`@everyone` ping. |
| 5 | **Credits** | Earn credits by messaging (weighted-random award) and by being in voice with others. Leaderboard + admin economy dashboard. |
| 6 | **Role shop** | Spend credits to buy roles. The catalogue lives in the DB and the owner edits it live with `/shop-add`/`/shop-remove` — no redeploy needed. Atomic deduction with automatic refund on failure. |
| 7 | **Minor services** | Spend a fixed amount of credits to file a service request to staff via a modal. |
| 8 | **Verify gate** | New members are held behind a hold role that hides every channel except the gate; a persistent button reveals the server and grants the verified role. New channels are auto-hidden from held members as the server grows. |
| 9 | **Honeypot channel** | One designated bait channel with zero legitimate traffic. Any message posted there triggers an automatic penalty (kick, ban, remove roles, timeout, or alert-only) — the classic sign of a compromised account or spam bot. Keeps only a bare trigger counter, by design: no per-incident log of who or when. |
| — | **Owner tools** | Private prefix commands (`!`) to grant/set/inspect credits, dump a full DB overview, back up the database, and restart the bot. Replies DM you privately by default; `!output here` switches them (and backups) to the channel. |
| — | **Leave cleanup** | When a member leaves: their open ticket closes immediately and their balance burns after a **24 h grace period** (rejoining in time keeps everything). |
| — | **Extras (drop-ins)** | Any `.py` file dropped into `cogs/extras/` auto-loads at startup — no registration needed. |

---

## Command reference

### Slash commands

| Command | Who | Purpose |
|---------|-----|---------|
| `/ticket-panel` | Staff | Post the "open ticket" button panel |
| `/close` | Staff | Close the current ticket (triggers a review) |
| `/redeem <code>` | Anyone | Redeem a code directly (role and/or credits) without the panel |
| `/redeem-panel` | Admin | Post the code-redemption panel (button + modal) |
| `/add-code [role] [credits] [code]` | Owner | Add one code granting a role and/or credits (auto-generates the code if omitted) |
| `/generate-codes <count> [role] [credits]` | Owner | Bulk-generate up to 50 codes with the same reward |
| `/list-codes [status] [role]` | Owner | List codes with their reward + redemption status |
| `/redemptions [user] [from] [to]` | Owner | Search the permanent redemption log by user/date, with pagination |
| `/delete-codes <amount> [role]` | Owner | Delete unused codes (redeemed codes are never touched) |
| `/code-stats` | Owner | Total / used / remaining code counts |
| `/reviews <on\|off>` | Admin | Turn the review system on or off |
| `/announce <title> <body> [image] [ping]` | Admin | Post an announcement |
| `/economy` | Admin | Economy dashboard: circulating supply + spend over 7d / 30d / 6mo / all-time |
| `/balance` | Anyone | Show your credit balance |
| `/pay <user> <amount>` | Anyone | Transfer credits to another member (fee burned, daily cap) |
| `/leaderboard` | Anyone | Top 10 credit holders (owner excluded) |
| `/shop` | Anyone | View the role shop |
| `/buy <role>` | Anyone | Buy a role with credits |
| `/shop-add <role> <cost> [name] [description]` | Owner | Add or edit a shop role live (no redeploy) |
| `/shop-remove <item>` | Owner | Remove a role from the shop |
| `/shop-list` | Owner | List the shop catalogue with prices |
| `/request-service` | Anyone | Spend credits to request a minor service |
| `/verify-setup <verify_role> <unverify_role>` | Admin | Post the verify panel in the current channel, hide every other channel from the hold role, and store the config |
| `/verify-status` | Admin | Show the configured roles/channel, on/off state, and verified member count |
| `/verify-enable` | Admin | Turn the verify gate back on |
| `/verify-disable` | Admin | Turn the verify gate off (new joiners are no longer held) |
| `/honeypot-setup <channel> <action> [duration]` | Owner | Designate the bait channel and the penalty (kick / ban / remove roles / timeout / alert-only); `duration` only applies to timeout |
| `/honeypot-status` | Owner | Show the configured channel, action, on/off state, trigger count, and exempt roles |
| `/honeypot-enable` | Owner | Turn the honeypot back on |
| `/honeypot-disable` | Owner | Turn the honeypot off |
| `/honeypot-action set <action> [duration]` | Owner | Change the configured penalty without a full re-setup |
| `/honeypot-safe-roles add <role>` | Owner | Exempt a role from the honeypot (messages are still deleted, no penalty applied) |
| `/honeypot-safe-roles remove <role>` | Owner | Remove a role from the exemption list |
| `/honeypot-safe-roles list` | Owner | List currently exempt roles |

> **Owner-gated slash commands:** the code commands (`/add-code`,
> `/generate-codes`, `/list-codes`, `/delete-codes`, `/code-stats`), the shop
> management commands (`/shop-add`, `/shop-remove`, `/shop-list`), and every
> honeypot command show up for admins because of Discord's default
> permissions, but the code actually checks for the **application owner**
> specifically. The verify-gate commands are the exception — those check for
> **admin**, not owner, so any admin can run them.

### Ticket admin menu (in-channel, staff-only)

Every ticket channel carries a persistent **🛠️ أدوات الإدارة** button
alongside the close button. Staff who press it get an ephemeral dropdown with
three actions:

| Action | What it does |
|--------|--------------|
| **طلب نسخة من التذكرة (copy)** | Flattens the channel history (up to 1,000 messages) into a plain-text transcript and DMs it to the requesting admin as a file. Falls back to posting it ephemerally in-channel if their DMs are closed. |
| **تذكير العضو (remind)** | Pings the ticket opener in-channel and best-effort DMs them a nudge that staff is waiting on a reply. |
| **إغلاق بسبب (close with reason)** | Opens a modal for a reason, then closes the ticket the same way `/close` does — DB row, review request, log entry — except the closure log and the opener's DM both include the stated reason. |

### Owner prefix commands (`!`)

Owner-only, invisible to everyone else. By **default** each deletes your message
and DMs the result back so nobody sees the command or its output. `!output here`
flips every owner command **and the `!dbbp` backup** to reply in the channel
instead (and keep your message); `!output private` restores the default. The
choice is stored in the DB, so it persists across restarts.

| Command | Purpose |
|---------|---------|
| `!give @user <amount>` | Add credits |
| `!take @user <amount>` | Remove credits (clamps at 0) |
| `!setcred @user <amount>` | Set an exact balance (aliases: `!setcredits`, `!set`) |
| `!credits @user` | Show a member's balance + recent purchases |
| `!purchases @user` | Full purchase history |
| `!db` | Full database snapshot (owner's own account excluded from economy totals) |
| `!dbbp` | Snapshot the live database (`VACUUM INTO`) and send it as a backup file — private DM by default, or into the channel per `!output` (aliases: `!dbbackup`, `!backup`) |
| `!output [here \| private]` | Choose where owner replies + backups go: the channel or a private DM (default). No argument shows the current mode (aliases: `!outputmode`, `!sendmode`) |
| `!tickreset` | Reset the ticket counter back to #1 (refuses while a ticket is open) |
| `!hpreset` | Zero the honeypot trigger counter (aliases: `!honeypotreset`) |
| `!restart` | Cleanly restart the bot process — flushes the DB, then re-execs in place (aliases: `!rest`, `!reboot`) |
| `!nvhelp` | List owner commands |

> **Careful with `!output here` + `!dbbp`:** the backup file is the entire
> economy (balances, tickets, reviews, codes). In channel mode anyone who can
> read that channel can download it — only use it in a private owner/staff channel.

**`!dmall <message>`** breaks the pattern: it's gated to the Developer-Portal
team instead of the owner, so anyone with team access can DM every human
member. It confirms with `yes`/`no` first, then sends in bursts with a random
20–30s gap between them so Discord doesn't flag it as spam. Typing `stop`
kills it at any point, and it kills itself automatically if Discord starts
bouncing sends. Unlike every owner command, it doesn't delete your message —
everyone in the channel sees it happen.

**Amount shorthand:** anywhere an owner or code credit amount is expected you can
type `1k`, `2.5m`, `1b`, or `1,000` instead of spelling out zeros — e.g.
`!give @user 1k` or `/add-code credits:2.5k`.

---

## How credits work

**Message earning** ([cogs/credits.py](cogs/credits.py))
- Weighted-random award (default 8–40), where higher amounts are exponentially
  rarer (`CREDIT_WEIGHT_LAMBDA`).
- Guards: messages must be **≥ 3 characters**, a per-user cooldown
  (`CREDIT_MSG_COOLDOWN`, default 210s), a per-minute cap
  (`CREDIT_MSG_MAX_PER_MIN`, default 4), and an ignored-channels list.

**Voice earning**
- A background task awards `CREDIT_VOICE_AMOUNT` (default 3) every
  `CREDIT_VOICE_INTERVAL` (default 180s).
- Only pays out when **at least 2 non-bot members** share a non-AFK voice
  channel; deafened/self-deafened members are skipped (anti-farm).

Redeemable codes and the owner commands can also grant credits directly.

---

## How the verify gate works

([cogs/verify.py](cogs/verify.py))

- `/verify-setup` picks two roles — a **hold role** and a **verified role** —
  and posts a one-button panel in the current channel. The bot then applies a
  deny-`View Channel` overwrite for the hold role to every channel in the
  server except the gate itself.
- New members get the hold role on join, so they see only the gate channel
  until they act.
- Pressing the panel's **تحقّق** button removes the hold role (which is what
  actually reveals the server — a role deny beats the `@everyone` allow) and
  grants the verified role.
- Any channel created after setup is auto-hidden from the hold role too, so
  the lockdown doesn't drift as the server grows.
- **Privacy by design:** no per-member verification record is kept — only the
  current config (roles, channel, panel message, on/off state).
- `/verify-setup` validates before touching anything: the two roles must
  differ, neither can be `@everyone`, and the bot's own top role must sit
  above both — otherwise it refuses outright rather than leaving the server
  half-configured.

---

## How the honeypot works

([cogs/honeypot.py](cogs/honeypot.py))

- `/honeypot-setup` designates one channel as bait and picks a penalty: kick,
  ban, remove roles, timeout (with a configurable duration up to Discord's
  28-day cap), or alert-only.
- Legitimate members have no reason to post in the bait channel, so any
  message there — typically from a compromised account or a spam bot —
  triggers the configured penalty automatically.
- A tracking embed in the channel shows the live trigger count and the
  currently configured action; it's edited in place (and self-heals if
  deleted) rather than reposted.
- `/honeypot-safe-roles` exempts specific roles from the penalty — their
  messages are still deleted, but no action is taken against the member.
- **By design, this feature keeps no per-incident record** — no log of who
  triggered it or when, only the bare running counter. That's a deliberate
  privacy trade-off, not an oversight: know that if you ever need to
  investigate *which* account tripped the trap and when, this system won't
  have that history for you.

---

## Architecture

```
bot.py            Entry point: logging, DB connect, load cogs, sync slash tree
config.py         All settings — reads DISCORD_TOKEN (secret) from env; every
                  other value (IDs/thresholds) is a hardcoded default here
database.py       Async SQLite layer: schema, migrations, atomic helpers
amounts.py        "1k"/"2.5m" amount parsing (converter + parser)
branding.py       Shared logo attachment + web-store link button
shop_config.json  Role-shop SEED only (first boot); live catalogue lives in DB
cogs/
  tickets.py        System 1 (+ in-ticket admin menu: transcript / remind / close-reason)
  codes.py          System 2
  reviews.py        System 3
  announcements.py  System 4
  credits.py        System 5 (+ /leaderboard, /economy)
  shop.py           System 6
  services.py       System 7
  verify.py         System 8 — join-verification gate
  honeypot.py        System 9 — bait channel + auto-penalty
  owner.py          Owner prefix commands
  extras/           Drop-in folder — every .py here auto-loads (see _template.py)
    dmall.py          !dmall — team-gated mass DM with burst/cooldown + stop
    dbbp.py           !dbbp — DM the owner a live database backup
    restart.py        !restart — clean in-place process restart
    usernames.py      Background: keeps the `users` table fresh (names in views)
    leavers.py        Background: ticket close on leave + 24 h balance burn
tools/
  seed_codes.py     CLI to bulk-insert codes into the DB
  stress_test.py    Scripted checks against the DB layer (economy, shop,
                    leavers, verify gate, backups, and more)
Assets/NVS-logo.orig.png
```

### Data model (SQLite)

`codes` (code → role_id / credits / redeemed state), `credits` (user_id →
balance), `tickets`, `reviews`, `purchases`, `transfers` (the `/pay` log),
`users` (user_id → username, kept fresh by the usernames drop-in), `leavers`
(pending 24 h balance burns — survives restarts), `shop_items` (the live
role-shop catalogue, seeded once from `shop_config.json`), `verify_config`
(single-row: verify/hold role IDs, gate channel, panel message ID, on/off
state), `honeypot_config` (single-row: bait channel, action type, timeout
duration, panel message ID, trigger count, on/off state),
`honeypot_safe_roles` (roles exempt from the honeypot penalty), and a
`settings` key/value table. A few of these decisions matter more than they
look:

- **Atomic mutations.** Credit spends run a conditional
  `UPDATE … WHERE balance >= ?`, so a balance can't go negative even under a
  race. Code redemption flips `redeemed = 0 → 1` the same way — no
  double-spend window.
- **Durability.** WAL journaling plus `synchronous=FULL`, because Railway
  kills containers with a bare SIGTERM and no graceful shutdown. Recent
  writes need to survive that.
- **Additive migrations.** `Database._migrate()` runs on connect and adds new
  columns with `ALTER TABLE` (e.g. `codes.credits`, later
  `verify_config.unverify_role_id`), so an existing database just upgrades in
  place — no manual intervention.
- **Timestamps** are ISO-8601 UTC strings, compared lexicographically for
  time-window aggregates. Works fine as long as the format never changes.
- **Readable views.** Six `v_*` views (balances, purchases, reviews, tickets,
  transfers, redemptions) join every table against `users`, so opening the DB
  — or a `!dbbp` backup — in a SQLite browser shows usernames instead of raw
  Discord IDs. They're dropped and recreated on every startup, so any
  definition change ships automatically.
- **Serialized transactions.** Multi-statement writes (transfers, backups,
  balance burns) run under a single asyncio lock. Has to be that way — the
  whole bot shares one SQLite connection.
- **Live backups.** `!dbbp` snapshots with `VACUUM INTO`, which is atomic,
  includes the WAL, and compacts the copy on the way out — safe to run while
  the bot is live.
- **Live role shop.** The catalogue is DB-backed and edited at runtime via
  `/shop-add`/`/shop-remove`. Buy buttons are discord.py `DynamicItem`s
  matched by `custom_id` pattern, so a role added today gets a working button
  immediately — even on `/shop` messages that were posted before the last
  restart.
- **Single config rows.** `verify_config` and `honeypot_config` are each
  pinned to a single row (`CHECK (id = 1)`); re-running setup repoints that
  row via `ON CONFLICT … DO UPDATE` instead of creating a duplicate, and
  re-arms the `enabled` flag in the process.

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then put your bot token in DISCORD_TOKEN
python bot.py
```

On first run the database and tables are created automatically, cogs load, and
slash commands sync to `GUILD_ID`.

**Requirements:** Python 3.11+, a Discord application/bot token, and the
**Server Members** + **Message Content** privileged intents enabled in the
Developer Portal.

See **[SETUP.md](SETUP.md)** for the full setup, required bot permissions,
seeding codes, and deploying on Railway with a persistent volume.

---

## Configuration

Everything is centralized in [config.py](config.py). **Only `DISCORD_TOKEN` is
a secret** — it has to come from the environment (`.env` locally, a Railway
Variable in production) and should never be committed. Every other value
(guild/role/channel IDs, plus tunables like credit amounts, cooldowns, and
service cost) has a default in `config.py`; set an env var with the same name
to override it.

The role-shop catalogue is **seeded** from
[shop_config.json](shop_config.json) — an array of
`{ role_id, name, cost, description }` objects — on the very first boot only.
After that, the `shop_items` table is the source of truth. Manage it live with
`/shop-add` and `/shop-remove`; editing the JSON after launch does nothing to
a store that's already running.

The verify gate and honeypot both work the same way as the role shop in this
respect: `/verify-setup` and `/honeypot-setup` write their config to the
database on first run, and later re-running the setup command repoints that
same row rather than creating a second configuration.

---

## Operational notes

- In-memory trackers (rate limits, cooldowns) reset on restart by design.
- Role assignments that fail on a permission error **auto-refund** the credits.
- The bot logs failures with context and never crashes on a single failed action.
- No public port is needed — run it as a background worker (`Procfile`: `worker: python bot.py`).
- Both the verify gate and the honeypot are deliberately record-light: verify
  keeps no per-member verification history, and the honeypot keeps only a
  running trigger count, not a log of individual incidents. Neither is a bug —
  both are stated privacy trade-offs — but it's worth knowing going in if you
  ever need to audit *who* tripped either system and *when*.

---

## License
[MIT](https://github.com/itsreallyhex/Night-Void-System/blob/main/LICENSE)