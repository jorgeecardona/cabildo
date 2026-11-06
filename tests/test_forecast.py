"""``forecast``: known answers on synthetic data."""

import random

import pytest

from cabildo import forecast
from cabildo.forecast import HOUR, History, Observation, Tokens, Weights

START = 1_000_000 * HOUR
TRUE = Weights(input=0.0, output=2e-4, cache_read=1e-6, cache_write=5e-5)


def window(seed, resets_at, *, extra=0.0, noise=0.0, weights=TRUE):
    """One window of hourly samples with independent token bursts."""
    rng = random.Random(seed)  # noqa: S311
    observations = []
    total = Tokens()
    for hour in range(60):
        total = Tokens(
            0,
            total.output + rng.randrange(0, 5000),
            total.cache_read + rng.randrange(0, 900_000),
            total.cache_write + rng.randrange(0, 30_000),
        )
        percent = weights.percent(total) + extra + rng.gauss(0, noise)
        observations.append(Observation(resets_at, START + hour * HOUR, percent, total))
    return observations


def test_fit_recovers_the_true_weights_and_is_usable():
    fit = forecast.fit_weights(window(1, 1.0))
    assert fit is not None
    assert fit.weights.output == pytest.approx(TRUE.output, rel=0.02)
    assert fit.weights.cache_write == pytest.approx(TRUE.cache_write, rel=0.02)
    assert fit.usable


def test_fit_reports_each_window_use_not_in_the_transcripts():
    fit = forecast.fit_weights(window(1, 1.0, extra=7.0) + window(2, 2.0))
    assert fit is not None
    assert fit.unexplained[1.0] == pytest.approx(7.0, abs=0.2)
    assert fit.unexplained[2.0] == pytest.approx(0.0, abs=0.2)


def test_percent_unrelated_to_tokens_is_not_usable_though_both_only_rise():
    # the percent rises steadily whatever the tokens do, so cumulative R2 would look excellent
    rng = random.Random(3)  # noqa: S311
    total = 0
    observations = []
    for hour in range(60):
        total += rng.choice((0, 0, 0, 20_000))
        observations.append(
            Observation(1.0, START + hour * HOUR, hour + rng.gauss(0, 0.3), Tokens(output=total))
        )
    fit = forecast.fit_weights(observations)
    assert fit is not None
    assert not fit.usable


def test_too_few_increments_is_not_usable():
    fit = forecast.fit_weights(window(1, 1.0)[:10])
    assert fit is not None
    assert fit.r2 is None
    assert not fit.usable


def test_no_observations_has_no_fit():
    assert forecast.fit_weights([]) is None


def test_history_keeps_idle_hours_as_zero():
    history = forecast.history_of({START: 1.0, START + 2 * HOUR: 3.0}, START + 3 * HOUR)
    assert history == History(START, (1.0, 0.0, 3.0))


def steady(burn, weeks=4):
    return History(START, (burn,) * (weeks * forecast.WEEK_HOURS))


def test_a_constant_burn_reaches_the_limit_at_the_exact_time():
    now = START + 4 * forecast.WEEK_HOURS * HOUR
    got = forecast.simulate(steady(1.0), 50.0, now, now + 100 * HOUR, paths=20)
    assert got is not None
    assert got.p_cutoff == 1.0
    assert got.cutoff_p50 == now + 50 * HOUR
    assert got.final_p50 == pytest.approx(150.0)


def test_no_burn_never_cuts_off():
    now = START + 4 * forecast.WEEK_HOURS * HOUR
    got = forecast.simulate(steady(0.0), 50.0, now, now + 100 * HOUR, paths=20)
    assert got is not None
    assert got.p_cutoff == 0.0
    assert got.cutoff_p50 is None


def test_a_task_that_does_not_fit_cuts_off_now():
    now = START + 4 * forecast.WEEK_HOURS * HOUR
    got = forecast.simulate(steady(0.0), 90.0, now, now + 10 * HOUR, task_percent=15.0, paths=20)
    assert got is not None
    assert got.p_cutoff == 1.0
    assert got.cutoff_p50 == now


def test_the_probability_matches_a_case_with_a_known_answer():
    # even weeks burn 10 per hour and odd weeks nothing, so each block is one or the other with
    # equal chance. From 50%, one block of 6 hours reaches 110 or stays at 50.
    burn = tuple(
        10.0 if (i // forecast.WEEK_HOURS) % 2 == 0 else 0.0
        for i in range(40 * forecast.WEEK_HOURS)
    )
    now = START + len(burn) * HOUR
    got = forecast.simulate(History(START, burn), 50.0, now, now + 6 * HOUR, paths=2000)
    assert got is not None
    assert got.p_cutoff == pytest.approx(0.5, abs=0.05)
    assert got.final_p10 == pytest.approx(50.0)
    assert got.final_p90 == pytest.approx(110.0)


def test_paths_use_the_same_hour_of_the_week():
    # only Monday 00:00-06:00 burns: a horizon on a Monday morning must see it, on Friday not
    week = [0.0] * forecast.WEEK_HOURS
    first = next(
        i for i in range(forecast.WEEK_HOURS) if forecast.hour_of_week(START + i * HOUR) == 0
    )
    for k in range(6):
        week[(first + k) % forecast.WEEK_HOURS] = 10.0
    history = History(START, tuple(week * 4))
    monday = START + first * HOUR + 4 * forecast.WEEK_HOURS * HOUR
    friday = monday + 96 * HOUR
    got_monday = forecast.simulate(history, 0.0, monday, monday + 6 * HOUR, paths=50)
    got_friday = forecast.simulate(history, 0.0, friday, friday + 6 * HOUR, paths=50)
    assert got_monday is not None
    assert got_friday is not None
    assert got_monday.final_p50 == pytest.approx(60.0)
    assert got_friday.final_p50 == pytest.approx(0.0)


def test_the_same_seed_gives_the_same_forecast():
    burn = tuple(random.Random(1).random() for _ in range(2000))  # noqa: S311
    history = History(START, burn)
    now = START + 2000 * HOUR
    assert forecast.simulate(history, 10.0, now, now + 50 * HOUR) == forecast.simulate(
        history, 10.0, now, now + 50 * HOUR
    )


def test_history_shorter_than_a_block_gives_no_forecast():
    assert forecast.simulate(History(START, (1.0,)), 0.0, START, START + HOUR) is None
