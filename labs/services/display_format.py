"""Display-only planning capital; execution and stored accounting stay exact."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING


def planning_capital(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    step = Decimal(10) ** max(0, amount.adjusted() - 1)
    return int((amount / step).to_integral_value(rounding=ROUND_CEILING) * step)
