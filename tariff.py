"""Magna 4T tariff: all-in import prices (incl. VAT and distribution/transit)."""

from datetime import datetime, timedelta, timezone

# (first hour, end hour exclusive, band, price with energy aid, price without energy aid)
# in EUR/kWh incl. VAT and transit, weekday prices. Ranges wrap past midnight when
# the first hour is greater than the end hour.
TARIFF_BANDS = (
    (22, 6, "night", 0.117, 0.156),
    (6, 9, "peak", 0.222, 0.261),
    (9, 13, "late_morning", 0.129, 0.167),
    (13, 19, "afternoon", 0.167, 0.206),
    (19, 22, "peak", 0.222, 0.261),
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


def _band(moment):
    if not isinstance(moment, datetime):
        raise ValueError("Tariff hour must be a datetime.")
    for start, end, name, with_aid, without_aid in TARIFF_BANDS:
        if _in_band(moment.hour, start, end):
            return name, with_aid, without_aid
    raise ValueError("Tariff bands do not cover every hour.")


def _weekend_adjusted(moment, price):
    if moment.weekday() >= 5:
        price -= WEEKEND_DISCOUNT * (price - TRANSIT_EUR_PER_KWH)
    return price


def band_name(moment):
    return _band(moment)[0]


def import_price(moment, energy_aid=True):
    """All-in import price for a local wall-clock datetime."""
    _, with_aid, without_aid = _band(moment)
    return _weekend_adjusted(moment, with_aid if energy_aid else without_aid)


def tariff_schedule(start, hours):
    """Hourly tariff from the local hour containing start, stepping in real (UTC) hours."""
    if not isinstance(start, datetime) or start.tzinfo is None:
        raise ValueError("Tariff schedule start must be a timezone-aware datetime.")
    local_zone = start.tzinfo
    first = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    schedule = []
    for offset in range(hours):
        local = (first + timedelta(hours=offset)).astimezone(local_zone)
        name, with_aid, without_aid = _band(local)
        schedule.append(
            {
                "time": local.isoformat(timespec="minutes"),
                "band": name,
                "with_aid_eur_per_kwh": round(_weekend_adjusted(local, with_aid), 4),
                "without_aid_eur_per_kwh": round(_weekend_adjusted(local, without_aid), 4),
            }
        )
    return schedule
