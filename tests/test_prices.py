"""``prices``: what a model's tokens cost, and what it refuses to guess at."""

from __future__ import annotations

import pytest

from cabildo import prices


def test_every_rate_is_a_positive_number():
    for model, p in prices.PRICES.items():
        rates = (p.input, p.cache_write_5m, p.cache_write_1h, p.cache_read, p.output)
        assert all(r > 0 for r in rates), model


def test_the_four_input_rates_keep_their_published_order():
    """Reading a cached token is the cheapest input and a 1-hour write the dearest. A table
    edit that broke that order would price a cache-heavy day wildly wrong in silence.
    """
    for model, p in prices.PRICES.items():
        assert p.cache_read < p.input < p.cache_write_5m < p.cache_write_1h, model
        assert p.input < p.output, model


def test_a_dated_model_id_prices_as_the_model():
    assert prices.price_of("claude-sonnet-4-5-20250929") is prices.PRICES["claude-sonnet-4-5"]


def test_an_unknown_model_has_no_price():
    assert prices.price_of("some-other-llm") is None
    assert prices.cost("some-other-llm", input_tokens=1_000_000, output_tokens=0) is None


def test_cost_adds_each_kind_of_token_at_its_own_rate():
    # Claude Opus 5.5: $4 input, $5 5m write, $8 1h write, $0.20 read, $20 output per MTok.
    got = prices.cost(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_write_5m=1_000_000,
        cache_write_1h=1_000_000,
        cache_read=1_000_000,
    )
    assert got == pytest.approx(4 + 5 + 8 + 0.20 + 20)


def test_a_fast_mode_reply_is_not_priced_at_standard_rates():
    """Fast mode bills at its own rates, which this table does not carry. None, so the
    caller reports the tokens and leaves the money out, rather than understating the bill.
    """
    assert (
        prices.cost("claude-opus-5-5", input_tokens=1_000, output_tokens=1_000, fast=True) is None
    )


def test_haiku_5_5_crosses_into_its_upper_tier_on_a_long_prompt():
    small = prices.price_of(prices.LONG_PROMPT_MODEL, long_prompt=False)
    large = prices.price_of(prices.LONG_PROMPT_MODEL, long_prompt=True)
    assert small is not None and large is not None
    assert large.input > small.input
    assert prices.is_long_prompt(prices.LONG_PROMPT_TOKENS) is False  # the boundary is above
    assert prices.is_long_prompt(prices.LONG_PROMPT_TOKENS + 1) is True


def test_only_haiku_5_5_has_a_second_tier():
    """`long_prompt` is a property of one model's published pricing, not a general rule."""
    assert prices.price_of("claude-opus-5-5", long_prompt=True) is prices.PRICES["claude-opus-5-5"]


def test_cost_puts_a_long_haiku_prompt_in_the_upper_tier_itself():
    """The caller passes token counts, not a tier: the tier is read off the input size."""
    over = prices.LONG_PROMPT_TOKENS + 1
    got = prices.cost(prices.LONG_PROMPT_MODEL, input_tokens=over, output_tokens=0)
    assert got == pytest.approx(over * prices.LONG_PROMPT_PRICE.input / prices.PER_MILLION)
