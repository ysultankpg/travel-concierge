"""Currency tools: live rates and explicit amount conversion.

Fixes vs. the original `get_exchange_rates`:
  * It attached an `Authorization: Bearer $CURRENCY_API_KEY` header to
    api.frankfurter.app, which is a keyless API — the header was dead code that
    implied a secret was needed.
  * It returned a raw Python dict repr of rates and no way to convert an actual
    amount, so the model did arithmetic in its head (unreliable for budgets).
  * An invalid currency code produced an opaque upstream error.
"""

from __future__ import annotations

import json

from . import http

_LATEST = "https://api.frankfurter.app/latest"
_CURRENCIES = "https://api.frankfurter.app/currencies"


def _clean(code: str) -> str:
    return code.strip().upper()


async def get_exchange_rates(
    base_currency: str = "USD",
    target_currencies: str = "EUR,JPY,GBP,INR,IDR,TZS",
) -> str:
    """Fetch live currency exchange rates.

    Args:
        base_currency: Base 3-letter ISO currency code (e.g. 'USD').
        target_currencies: Comma-separated target ISO codes (e.g. 'EUR,JPY').
            Pass an empty string for every available currency.

    Returns:
        JSON with the base currency, the reference date, and a rates map.
    """
    base = _clean(base_currency) or "USD"
    params: dict[str, str] = {"from": base}
    if target_currencies.strip():
        targets = [_clean(c) for c in target_currencies.split(",") if c.strip()]
        # Frankfurter 422s if the base appears in the target list.
        targets = [t for t in targets if t != base]
        if targets:
            params["to"] = ",".join(targets)

    data = await http.get_json(_LATEST, params=params)
    if data.get("error"):
        return json.dumps(
            {"error": f"Could not fetch exchange rates: {data['error']}"}
        )
    return json.dumps(
        {
            "base": data.get("base", base),
            "as_of": data.get("date"),
            "rates": data.get("rates", {}),
            "note": "Reference rates from the European Central Bank; not a "
            "dealing rate. Cards and bureaux add a 1-4% spread.",
        }
    )


async def convert_currency(
    amount: float, from_currency: str, to_currency: str
) -> str:
    """Convert a specific amount between two currencies at the live rate.

    Use this for budget maths instead of calculating by hand, so the number the
    traveller sees is exact.

    Args:
        amount: The amount to convert (e.g. 1250.50).
        from_currency: Source 3-letter ISO code (e.g. 'USD').
        to_currency: Target 3-letter ISO code (e.g. 'JPY').

    Returns:
        JSON with the converted amount, the rate used, and the rate date.
    """
    src, dst = _clean(from_currency), _clean(to_currency)
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return json.dumps({"error": f"'{amount}' is not a valid amount."})
    if value < 0:
        return json.dumps({"error": "Amount must not be negative."})

    if src == dst:
        return json.dumps(
            {
                "amount": value,
                "from": src,
                "to": dst,
                "converted": round(value, 2),
                "rate": 1.0,
                "note": "Same currency; no conversion applied.",
            }
        )

    data = await http.get_json(_LATEST, params={"from": src, "to": dst})
    if data.get("error"):
        return json.dumps({"error": f"Conversion failed: {data['error']}"})

    rate = (data.get("rates") or {}).get(dst)
    if rate is None:
        supported = await http.get_json(_CURRENCIES)
        known = sorted(supported.keys()) if not supported.get("error") else []
        return json.dumps(
            {
                "error": f"No rate available for {src}->{dst}.",
                "supported_currencies": known,
            }
        )

    return json.dumps(
        {
            "amount": value,
            "from": src,
            "to": dst,
            "converted": round(value * rate, 2),
            "rate": rate,
            "as_of": data.get("date"),
        }
    )
