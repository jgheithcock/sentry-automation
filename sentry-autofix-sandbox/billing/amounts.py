"""Money helpers used by the invoice importer."""
from decimal import Decimal, ROUND_HALF_UP


def parse_amount(text: str) -> float:
    """Parse a user-entered amount such as "$12.50" or "1,234.50" into a number."""
    text = text.strip().lstrip("$")
    return float(text)


def invoice_total(line_items: list[str], tax_rate: float = 0.0) -> float:
    """Sum the line-item amounts and apply tax, rounded to cents."""
    subtotal = sum(parse_amount(item) for item in line_items)
    total = Decimal(str(subtotal)) * (Decimal("1") + Decimal(str(tax_rate)))
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
