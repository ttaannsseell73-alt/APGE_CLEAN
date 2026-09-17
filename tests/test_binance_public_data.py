import pytest
from decimal import Decimal

from apge.binance_public_data import candles_only, parse_usdm_klines


def _row(open_time, o="100", h="102", l="99", c="101"):
    return [open_time, o, h, l, c, "10", open_time + 299999, "0", 1, "0", "0", "0"]


def test_parse_closed_klines_drops_latest_by_default():
    parsed = parse_usdm_klines([_row(1000), _row(301000), _row(601000)])
    assert len(parsed) == 2
    candles = candles_only(parsed)
    assert candles[0].open == Decimal("100")
    assert candles[0].close == Decimal("101")


def test_parser_can_keep_last_when_caller_knows_it_is_closed():
    parsed = parse_usdm_klines([_row(1000), _row(301000)], drop_last=False)
    assert len(parsed) == 2


@pytest.mark.parametrize(
    "rows",
    [
        [[1, "100"]],
        [_row(1000), _row(1000)],
        [_row(1000, h="98", l="99")],
        [_row(1000, o="103")],
        [_row(1000, c="98")],
        [_row(1000, o="NaN")],
        [_row(1000, o="-1")],
    ],
)
def test_parser_fails_closed_on_malformed_rows(rows):
    with pytest.raises(ValueError):
        parse_usdm_klines(rows, drop_last=False)
