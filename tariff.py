"""Magna 4T tariff: all-in import prices (incl. VAT and distribution/transit)."""

from datetime import datetime

# (first hour, end hour exclusive, price with energy aid, price without energy aid)
# in EUR/kWh incl. VAT and transit, weekday prices. Ranges wrap past midnight when
# the first hour is greater than the end hour.
TARIFF_BANDS = (
    (22, 6, 0.117, 0.156),
    (6, 9, 0.222, 0.261),
    (9, 13, 0.129, 0.167),
    (13, 19, 0.167, 0.206),
    (19, 22, 0.222, 0.261),
)

# Weekend discount applies to the energy component only, not to distribution fees.
WEEKEND_DISCOUNT = 0.12
# Estimated transit/distribution component, EUR/kWh incl. VAT. Derived from the published
# table and the 2026 energy price (night 0.0829 EUR/kWh excl. VAT at about 1.25 VAT factor):
# 0.156 - 1.25 * 0.0829 = 0.0524. The table alone cannot separate it exactly.
TRANSIT_EUR_PER_KWH = 0.052


def _in_band(hour, start, end):
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def import_price(moment, energy_aid=True):
    """All-in import price for a local wall-clock datetime."""
    if not isinstance(moment, datetime):
        raise ValueError("Tariff hour must be a datetime.")
    for start, end, with_aid, without_aid in TARIFF_BANDS:
        if _in_band(moment.hour, start, end):
            price = with_aid if energy_aid else without_aid
            break
    else:
        raise ValueError("Tariff bands do not cover every hour.")
    if moment.weekday() >= 5:
        price -= WEEKEND_DISCOUNT * (price - TRANSIT_EUR_PER_KWH)
    return price
