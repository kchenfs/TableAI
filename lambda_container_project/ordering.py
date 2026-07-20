"""Pure order pricing/resolution — no AWS, no Lex, unit-testable."""
import re
from decimal import Decimal, ROUND_HALF_UP

TAX_RATE = Decimal("0.13")


class UnknownMenuItem(Exception):
    def __init__(self, item_name):
        self.item_name = item_name
        super().__init__(f"Unknown menu item: {item_name}")


def _normalize_name(s):
    if not isinstance(s, str):
        return ""
    return re.sub(r"\s+", " ", s.strip().lower())


def _option_modifier(raw_item, group, choice):
    """Return the priceModifier (Decimal) for a chosen option, else 0."""
    for opt_group in raw_item.get("Options", []) or []:
        if _normalize_name(opt_group.get("name")) == _normalize_name(group):
            for opt in opt_group.get("items", []) or []:
                if _normalize_name(opt.get("name")) == _normalize_name(choice):
                    return Decimal(str(opt.get("priceModifier", 0)))
    return Decimal("0")


def resolve_and_price(parsed_items, menu_lookup):
    """Resolve parsed chat items against the menu; return (order_items, total_cents).

    order_items are website-shaped dicts; total_cents is the tax-inclusive total.
    Raises UnknownMenuItem if any item cannot be resolved by exact normalized name.
    """
    order_items = []
    subtotal = Decimal("0")

    for line in parsed_items:
        name = line.get("item_name", "")
        key = _normalize_name(name)
        entry = menu_lookup.get(key)
        if not entry:
            raise UnknownMenuItem(name)

        raw = entry["raw_item"]
        quantity = int(line.get("quantity", 1))
        if quantity < 1 or quantity > 50:
            raise UnknownMenuItem(name)  # treat absurd qty as unusable

        unit_price = Decimal(str(raw.get("Price", "0")))
        options = line.get("options") or {}
        option_parts = []
        for group, choice in options.items():
            unit_price += _option_modifier(raw, group, choice)
            option_parts.append(f"{group}: {choice}")

        line_subtotal = unit_price * quantity
        subtotal += line_subtotal

        order_items.append({
            "name": raw.get("ItemName", name),
            "quantity": quantity,
            "price": float(unit_price),
            "subtotal": float(line_subtotal),
            "options": "; ".join(option_parts),
            "id": raw.get("ItemNumber"),
            "location": raw.get("Location", ""),
        })

    total = subtotal * (Decimal("1") + TAX_RATE)
    total_cents = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return order_items, total_cents
