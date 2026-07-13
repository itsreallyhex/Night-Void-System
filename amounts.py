"""Human-friendly amount parsing.

Lets admins type shorthand like ``1k``, ``2.5m`` or ``1,000`` anywhere a credit
amount is expected instead of spelling out every zero. Exposes ``parse_amount``
plus two thin adapters: a prefix-command converter and a slash Transformer.
"""

import re

from discord.ext import commands

# k = thousand, m = million, b = billion.
_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_PATTERN = re.compile(r"^([0-9]*\.?[0-9]+)([kmb]?)$", re.IGNORECASE)


def parse_amount(text: str) -> int:
    """Parse '1000', '1k', '2.5m', '1,000' into an int number of credits.

    Raises ValueError on anything that isn't a whole, non-negative amount.
    """
    raw = text.strip().replace(",", "").replace("_", "").replace(" ", "")
    match = _PATTERN.match(raw)
    if not match:
        raise ValueError(f"invalid amount: {text!r}")
    number, suffix = match.group(1), match.group(2).lower()
    value = float(number) * _SUFFIXES.get(suffix, 1)
    if value != int(value):
        raise ValueError(f"amount must be a whole number of credits: {text!r}")
    return int(value)


class AmountConverter(commands.Converter):
    """Prefix-command converter: '1k' -> 1000. Bad input raises BadArgument."""

    async def convert(self, ctx: commands.Context, argument: str) -> int:
        try:
            return parse_amount(argument)
        except ValueError as exc:
            raise commands.BadArgument(str(exc)) from exc
