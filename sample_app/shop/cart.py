"""A minimal shopping cart."""

from shop.pricing import apply_discount, average_price


class Cart:
    def __init__(self) -> None:
        self._items: list[tuple[str, float]] = []

    def add(self, name: str, price: float) -> None:
        if price < 0:
            raise ValueError("price must be non-negative")
        self._items.append((name, price))

    def subtotal(self) -> float:
        return sum(price for _, price in self._items)

    def total(self, discount_percent: float = 0) -> float:
        """Cart total after an optional percentage discount."""
        return apply_discount(self.subtotal(), discount_percent)

    def average_item_price(self) -> float:
        return average_price([price for _, price in self._items])
