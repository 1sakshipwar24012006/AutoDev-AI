import pytest

from shop.pricing import apply_discount, average_price


def test_apply_discount_basic():
    assert apply_discount(100, 20) == pytest.approx(80)


def test_apply_discount_zero_percent_keeps_price():
    assert apply_discount(50, 0) == pytest.approx(50)


def test_apply_discount_full_discount_is_free():
    assert apply_discount(50, 100) == pytest.approx(0)


def test_apply_discount_rejects_invalid_percent():
    with pytest.raises(ValueError):
        apply_discount(10, 150)


def test_average_price():
    assert average_price([10, 20, 30]) == pytest.approx(20)


def test_average_price_empty_list_is_zero():
    assert average_price([]) == 0.0
