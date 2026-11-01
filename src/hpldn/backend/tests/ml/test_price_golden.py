"""
The golden set: five real homes priced end to end with the served model, for a sale on 27/09/2026.

Three were reported priced wrongly -- a W11 terrace whose sale five months
earlier barely moved its price, and two £3m+ homes priced well below their
local evidence -- and two were reported priced well and must stay so. Each
price is printed with its range, the stratum the range came from and the
weight the home's own previous sale carried. Reads hpdbldn (read-only) with
the pinned model (`ml/conftest.py`'s `pricing_service`, shared with
`test_hpp_inference`'s integration tier); fails without them.
"""

from datetime import date

import pytest

from hpldn.backend.scripts.hpp_inference import PropertyAddress

SALE_DATE = date(2026, 9, 27)
TODAY = date(2026, 9, 26)

GOLDEN = {
    "queensdale": PropertyAddress("11D", "QUEENSDALE ROAD", "LONDON", "W11 4QF"),
    "addison": PropertyAddress("56", "ADDISON AVENUE", "LONDON", "W11 4QP"),
    "sw3": PropertyAddress("1", None, "LONDON", "SW3 1AB"),
    "bedford": PropertyAddress("17", "BEDFORD GARDENS", "LONDON", "W8 7EF"),
    "gunnersbury": PropertyAddress("14", "GUNNERSBURY CRESCENT", "LONDON", "W3 9AA"),
}

# What the served model quoted without the recent-sale anchor and the luxury
# adjustments, for the two homes that were priced well.
BEFORE = {"bedford": 5_293_112.0, "gunnersbury": 878_562.0}

# Queensdale's April 2026 sale of £2.5m, moved to the sale date along W11
# terraces' local index.
QUEENSDALE_INDEXED_SALE = 2_530_000.0


def _money(value) -> str:
    return "-" if value is None else f"£{value:,.0f}"


@pytest.fixture(scope="module")
def golden(pricing_service, pinned_model_path):
    # The ranges and strata asserted below come from the bundle's calibration
    # (hpp_inference.calibration_path); without it every range is None.
    from hpldn.backend.scripts.hpp_inference import calibration_path

    calibration = calibration_path(pinned_model_path)
    if not calibration.is_file():
        pytest.fail(f"missing prerequisite: the served model's calibration {calibration}", pytrace=False)
    service = pricing_service
    prices = {name: service.price(address, SALE_DATE, today=TODAY) for name, address in GOLDEN.items()}
    for name, p in prices.items():
        print(
            f"{name:<12} {_money(p.value):>13}  range {_money(p.low)}-{_money(p.high)}  stratum {p.range_stratum}  "
            f"weight {p.extra.get('repeat_sale_weight')}  anchored {p.anchored_on_last_sale}  "
            f"nowcast {_money(p.extra.get('nowcast'))}  last sale {p.last_sale_date} {_money(p.last_sale_price)}"
        )
    return prices


def test_a_recent_sale_dominates_the_price(golden):
    queensdale = golden["queensdale"]
    assert queensdale.anchored_on_last_sale
    assert queensdale.extra["repeat_sale_weight"] >= 0.9
    assert queensdale.value == pytest.approx(QUEENSDALE_INDEXED_SALE, rel=0.05)
    assert queensdale.range_stratum.startswith("recent_sale:")
    # Narrower than the ±20 % the model's own W11 terrace stratum gave it.
    assert (queensdale.high - queensdale.low) / queensdale.value < 0.40


def test_old_luxury_sales_move_toward_their_local_evidence(golden):
    assert golden["addison"].value >= 6_300_000
    sw3 = golden["sw3"]
    # Not cut by SW3's sub-£2m flats.
    assert sw3.range_stratum != "district:SW3|F"
    assert sw3.value > 3_040_174
    for name in ("addison", "sw3", "bedford"):
        assert not golden[name].anchored_on_last_sale


def test_homes_priced_well_stay_close_to_before(golden):
    for name, before in BEFORE.items():
        assert golden[name].value == pytest.approx(before, rel=0.07), name
