from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Literal

Unit = Literal["кг", "шт"]

_UNIT_FACTORS = {
    **dict.fromkeys(
        (
            "кг",
            "килограмм",
            "килограмма",
            "килограммов",
            "л",
            "литр",
            "литра",
            "литров",
        ),
        Decimal(1),
    ),
    **dict.fromkeys(
        (
            "г",
            "гр",
            "грамм",
            "грамма",
            "граммов",
            "мл",
            "миллилитр",
            "миллилитра",
            "миллилитров",
        ),
        Decimal("0.001"),
    ),
    **dict.fromkeys(
        ("чл", "чайнаяложка", "чайныеложки", "чайныхложек"), Decimal("0.005")
    ),
    **dict.fromkeys(
        ("стл", "столоваяложка", "столовыеложки", "столовыхложек"), Decimal("0.015")
    ),
    **dict.fromkeys(("стакан", "стакана", "стаканов"), Decimal("0.2")),
}
_PIECES = {"шт", "штука", "штуки", "штук"}
_MAX_QUANTITY = Decimal(40)
_LIMIT_WARNING = "количество ограничено 40"
_GRAM_TOLERANCE = Decimal("0.000000000001")


def normalize_quantity(quantity: Decimal, unit: str) -> tuple[Decimal, Unit] | None:
    if not quantity.is_finite() or quantity <= 0:
        return None
    unit_key = "".join(unit.casefold().replace(".", "").split())
    if unit_key in _PIECES:
        return quantity, "шт"
    factor = _UNIT_FACTORS.get(unit_key)
    return None if factor is None else (quantity * factor, "кг")


def cart_quantity(
    amount: Decimal, ingredient_unit: Unit, product_unit: Unit, weight: Decimal | None
) -> tuple[Decimal, str | None] | None:
    if ingredient_unit == product_unit == "кг":
        q = amount.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    elif ingredient_unit == product_unit == "шт":
        q = amount.to_integral_value(rounding=ROUND_CEILING)
    elif ingredient_unit == "кг" and weight is not None:
        # Хвост двоичного числа в каталоге не должен добавлять лишнюю упаковку.
        gram_weight = (
            weight.scaleb(3).to_integral_value(rounding=ROUND_HALF_UP).scaleb(-3)
        )
        if gram_weight > 0 and abs(weight - gram_weight) <= _GRAM_TOLERANCE:
            weight = gram_weight
        if amount > weight * _MAX_QUANTITY:
            return _MAX_QUANTITY, _LIMIT_WARNING
        q = (amount / weight).to_integral_value(rounding=ROUND_CEILING)
    else:
        return None
    return (_MAX_QUANTITY, _LIMIT_WARNING) if q > _MAX_QUANTITY else (q, None)
