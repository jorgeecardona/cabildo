"""What a model's tokens cost, so a token count can be read as money.

Prices are US dollars per million tokens, copied from Anthropic's published pricing page
(https://platform.claude.com/docs/en/about-claude/pricing), and they are a snapshot. Two
consequences, both deliberate:

  * a model that is repriced after this table was written is priced wrong here until
    someone updates it, so the table carries the date it was taken;
  * a model the table does not know is reported as tokens with **no** dollar figure, never
    priced by analogy with a similar name.

A stale table degrades to silence, not to a confident wrong number. The same rule covers
fast mode, which bills at its own rates: a fast-mode response is counted in tokens and
left out of the dollar figure rather than priced as if it were standard.

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: When the rates below were taken from the pricing page. Shown wherever a cost is, so a
#: figure from an old install is readable as old rather than as current.
TAKEN_ON = "2026-11-14"

PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Price:
    """One model's rates, in dollars per million tokens.

    The four input rates are genuinely different prices for the same tokens, not a
    discount applied to one: writing a 5-minute cache entry costs more than plain input,
    writing a 1-hour entry more again, and reading either back costs a fraction. Summing
    them into a single "input" rate is the mistake this dataclass exists to prevent.
    """

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


#: Model id -> rates. Keys are the exact ids Claude Code writes into a transcript's
#: `message.model`. Retired models stay: an old transcript still names them.
PRICES: dict[str, Price] = {
    "claude-fable-5-2": Price(8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-mythos-5-2": Price(8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-fable-5-1": Price(10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-mythos-5-1": Price(10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-fable-5": Price(10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-mythos-5": Price(10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-opus-5-5": Price(4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-opus-5": Price(5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8": Price(5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-7": Price(5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-6": Price(5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-5": Price(5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-1": Price(15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-opus-4": Price(15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-sonnet-5-5": Price(2.0, 2.50, 4.0, 0.10, 10.0),
    "claude-sonnet-5": Price(2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-sonnet-4-6": Price(3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-5": Price(3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4": Price(3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-haiku-5-5": Price(0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5": Price(1.0, 1.25, 2.0, 0.10, 5.0),
}

#: Claude Haiku 5.5 alone is priced in two tiers: everything above this many input tokens
#: in one request bills at five times the rates above. Every other model has one rate.
LONG_PROMPT_MODEL = "claude-haiku-5-5"
LONG_PROMPT_TOKENS = 100_000
LONG_PROMPT_PRICE = Price(0.50, 0.625, 1.0, 0.05, 2.50)

#: Claude Code writes dated ids for some models (`claude-sonnet-4-5-20250929`). The date
#: is a snapshot of the same priced model, so it is stripped before the lookup.
_DATED = re.compile(r"-\d{8}$")


def normalize(model: str) -> str:
    """A transcript's model id reduced to the id the table is keyed by."""
    return _DATED.sub("", (model or "").strip())


def price_of(model: str, *, long_prompt: bool = False) -> Price | None:
    """The rates for one model, or None when the table does not know it.

    ``long_prompt`` picks Claude Haiku 5.5's over-100K tier and is ignored for every other
    model, none of which has a second tier. Use ``is_long_prompt`` to decide it from a
    request's input, and note that pricing a group of requests that share a model, a speed
    and a tier gives the same total as pricing each one: the rates are linear in tokens.
    """
    name = normalize(model)
    if long_prompt and name == LONG_PROMPT_MODEL:
        return LONG_PROMPT_PRICE
    return PRICES.get(name)


def is_long_prompt(input_tokens: int) -> bool:
    """Whether a request of this input size falls in Claude Haiku 5.5's upper tier.

    ``input_tokens`` is everything billed as input: plain, cache writes and cache reads.
    """
    return input_tokens > LONG_PROMPT_TOKENS


def cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    cache_read: int = 0,
    fast: bool = False,
) -> float | None:
    """What one request cost in dollars, or None when it cannot be priced honestly.

    None for a model the table does not know, and for a fast-mode response, which bills
    at rates this table does not carry. A caller that gets None must report the tokens
    and leave the money out, not substitute a zero -- a zero reads as "this was free".
    """
    if fast:
        return None
    billed_input = input_tokens + cache_write_5m + cache_write_1h + cache_read
    rates = price_of(model, long_prompt=is_long_prompt(billed_input))
    if rates is None:
        return None
    total = (
        input_tokens * rates.input
        + cache_write_5m * rates.cache_write_5m
        + cache_write_1h * rates.cache_write_1h
        + cache_read * rates.cache_read
        + output_tokens * rates.output
    )
    return total / PER_MILLION
