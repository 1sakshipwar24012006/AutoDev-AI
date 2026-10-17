# Discounts return the discount amount instead of the discounted price

`shop.pricing.apply_discount(100, 20)` returns `20.0` but should return `80.0`.
This also makes `Cart.total(discount_percent=20)` wrong, and a 0% discount makes the cart total 0.

Separately, `shop.pricing.average_price([])` raises `ZeroDivisionError`
(and so does `Cart().average_item_price()`); an empty list should average to `0.0`.

Please fix both in `shop/pricing.py` and add regression tests.
