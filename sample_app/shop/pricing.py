"""Pricing helpers."""


def apply_discount(price: float, percent: float) -> float:
    """Return ``price`` after applying a ``percent`` discount (0-100)."""
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return price * (percent / 100)


def average_price(prices: list[float]) -> float:
    """Return the average of ``prices``; an empty list averages to 0.0."""
    return sum(prices) / len(prices)
