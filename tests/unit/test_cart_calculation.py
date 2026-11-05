from decimal import Decimal

import pytest


@pytest.mark.parametrize(
    ("value", "unit", "amount", "normalized"),
    [
        ("5", " гр. ", "0.005", "кг"),
        ("250", "мл", "0.250", "кг"),
        ("0.1", "л", "0.1", "кг"),
        ("1", "ч. л.", "0.005", "кг"),
        ("2", "ст.л.", "0.030", "кг"),
        ("2", "стакана", "0.4", "кг"),
        ("1.5", "шт.", "1.5", "шт"),
    ],
)
def test_normalize_quantity(value, unit, amount, normalized):
    from receipes.cart.calculation import normalize_quantity

    assert normalize_quantity(Decimal(value), unit) == (Decimal(amount), normalized)


@pytest.mark.parametrize(
    ("amount", "weight", "expected"),
    [
        ("1.4", "1.3999999999999999", 1),
        ("1.4", "1.399999999999", 1),
        ("1.4", "1.3999999999989", 2),
        ("0.000000000001", "0.000000000001", 1),
        ("0.0004", "0.0004", 1),
    ],
)
def test_pack_weight_normalization_only_near_positive_whole_gram(
    amount, weight, expected
):
    from receipes.cart.calculation import cart_quantity

    result, _ = cart_quantity(Decimal(amount), "кг", "шт", Decimal(weight))
    assert result == expected
