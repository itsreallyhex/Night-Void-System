"""Utility to generate and insert redemption codes.

Usage:
    python tools/seed_codes.py --role 123456789012345678 --count 20
    python tools/seed_codes.py --credits 5000 --count 10
    python tools/seed_codes.py --role 123... --credits 5000

Reuses the bot's own generator (cogs.codes.generate_code) so seeded codes match
the NIGHTVOID-xxxxxxxxxxxxxxx format the redeem modal validates — an earlier
version of this tool produced 10-char bodies the bot rejected as malformed.
Prints every generated code to stdout.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running from repo root or tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from cogs.codes import CODE_CREDIT_MAX, generate_code  # noqa: E402
from database import Database  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed redemption codes.")
    parser.add_argument("--role", type=int, default=0, help="Role ID to link codes to.")
    parser.add_argument(
        "--credits", type=int, default=0,
        help=f"Credits each code grants (max {CODE_CREDIT_MAX:,}).",
    )
    parser.add_argument("--count", type=int, default=1, help="Number of codes to generate.")
    args = parser.parse_args()

    if not args.role and not args.credits:
        parser.error("specify --role and/or --credits (a code must grant something)")
    if not (0 <= args.credits <= CODE_CREDIT_MAX):
        parser.error(f"--credits must be between 0 and {CODE_CREDIT_MAX:,}")
    if args.count < 1:
        parser.error("--count must be at least 1")

    db = Database(config.DATABASE_PATH)
    await db.connect()

    created: list[str] = []
    for _ in range(args.count):
        code = generate_code()
        # Retry on the astronomically unlikely collision.
        while not await db.add_code(code, args.role, args.credits):
            code = generate_code()
        created.append(code)
    await db.close()

    reward = " + ".join(
        part for part in (
            f"role {args.role}" if args.role else "",
            f"{args.credits:,} credits" if args.credits else "",
        ) if part
    )
    print(f"Generated {len(created)} code(s) granting {reward}:")
    for code in created:
        print(code)


if __name__ == "__main__":
    asyncio.run(main())
