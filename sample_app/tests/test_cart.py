import pytest

from shop.cart import Cart


def make_cart():
    cart = Cart()
    cart.add("book", 20)
    cart.add("pen", 5)
    return cart


def test_subtotal():
    assert make_cart().subtotal() == 25


def test_total_without_discount():
    assert make_cart().total() == pytest.approx(25)


def test_total_with_discount():
    assert make_cart().total(discount_percent=20) == pytest.approx(20)


def test_average_item_price_of_empty_cart():
    assert Cart().average_item_price() == 0.0
