"""Pure order pricing/resolution — no AWS, no Lex, unit-testable."""
import re
import random
import string
from datetime import datetime
from zoneinfo import ZoneInfo
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


def _option_modifier(raw_item, group, choice, mode="takeout"):
    """Return the priceModifier (Decimal) for a chosen option, else 0.

    In dine-in mode prefers dineInPriceModifier; an absent value means
    "same as priceModifier", which is how an unmigrated option is represented.
    """
    for opt_group in raw_item.get("Options", []) or []:
        if _normalize_name(opt_group.get("name")) == _normalize_name(group):
            for opt in opt_group.get("items", []) or []:
                if _normalize_name(opt.get("name")) == _normalize_name(choice):
                    if mode == "dine-in" and opt.get("dineInPriceModifier") is not None:
                        return Decimal(str(opt.get("dineInPriceModifier")))
                    return Decimal(str(opt.get("priceModifier", 0)))
    return Decimal("0")


def resolve_and_price(parsed_items, menu_lookup, mode="takeout"):
    """Resolve parsed chat items against the menu; return (order_items, total_cents).

    order_items are website-shaped dicts; total_cents is the tax-inclusive total.
    Raises UnknownMenuItem if any item cannot be resolved by exact normalized name.

    mode is 'dine-in' or 'takeout'. Only an explicit 'dine-in' selects dine-in
    pricing, so an unexpected value falls back to takeout - the higher, current
    price - rather than silently undercharging.
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
        # UnknownMenuItem means "this line cannot be turned into a valid, priced
        # order line" — it covers both an unresolvable item and an unusable
        # quantity (non-numeric, or out of the sane 1..50 range). A malformed
        # quantity from the LLM parse must fail closed with this documented
        # exception, not leak a raw ValueError/TypeError to the caller.
        try:
            quantity = int(line.get("quantity", 1))
        except (TypeError, ValueError):
            raise UnknownMenuItem(name)
        if quantity < 1 or quantity > 50:
            raise UnknownMenuItem(name)

        # An absent DineInPrice means "same as takeout" - that is how an
        # unmigrated item is represented, so it must fall back, not fail.
        if mode == "dine-in" and raw.get("DineInPrice") is not None:
            unit_price = Decimal(str(raw.get("DineInPrice")))
        else:
            unit_price = Decimal(str(raw.get("Price", "0")))
        options = line.get("options") or {}
        option_parts = []
        for group, choice in options.items():
            unit_price += _option_modifier(raw, group, choice, mode)
            option_parts.append(f"{group}: {choice}")

        line_subtotal = unit_price * quantity
        subtotal += line_subtotal

        # ItemNumber comes from DynamoDB as a Decimal, which is not JSON
        # serializable — the order dict is later json.dumps'd for SNS/put_item.
        # Coerce to str, matching the website's records (id stored as e.g. "96").
        item_number = raw.get("ItemNumber")
        order_items.append({
            "name": raw.get("ItemName", name),
            "quantity": quantity,
            "price": float(unit_price),
            "subtotal": float(line_subtotal),
            "options": "; ".join(option_parts),
            "id": str(item_number) if item_number is not None else None,
            "location": raw.get("Location", ""),
        })

    total = subtotal * (Decimal("1") + TAX_RATE)
    total_cents = int((total * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return order_items, total_cents


def new_order_id(prefix):
    """Generate an order ID: {prefix}-{5 random uppercase alphanumeric characters}."""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{suffix}"


def _now_iso():
    """Return current time as ISO string in America/Toronto timezone."""
    return datetime.now(ZoneInfo("America/Toronto")).isoformat()


def build_dinein_order(order_id, table_id, items, total_cents, notes):
    """Build a dine-in order dict shaped for SNS/DynamoDB.

    Args:
        order_id: The order ID (e.g., "DINE-ABCDE")
        table_id: The table ID (e.g., "table-2")
        items: List of order items (resolved by resolve_and_price)
        total_cents: Total in cents (including tax)
        notes: Order notes (optional)

    Returns:
        dict with orderId, orderType, orderStatus, paymentStatus, tableId, items, notes, total, orderDate
    """
    return {
        "orderId": order_id,
        "orderType": "dine-in",
        "orderStatus": "PENDING_KITCHEN",
        "paymentStatus": "Dine-In",
        "tableId": table_id,
        "items": items,
        "notes": notes or "",
        "total": total_cents / 100,
        "orderDate": _now_iso(),
    }


def build_takeout_order(order_id, items, total_cents, notes):
    """Build a takeout order dict shaped for SNS/DynamoDB.

    Args:
        order_id: The order ID (e.g., "TKOT-ABCDE")
        items: List of order items (resolved by resolve_and_price)
        total_cents: Total in cents (including tax)
        notes: Order notes (optional)

    Returns:
        dict with orderId, orderType, orderStatus, paymentStatus, items, notes, total, orderDate
        (no tableId for takeout orders)
    """
    return {
        "orderId": order_id,
        "orderType": "takeout",
        "orderStatus": "PENDING_PAYMENT",
        "paymentStatus": "UNPAID",
        "items": items,
        "notes": notes or "",
        "total": total_cents / 100,
        "orderDate": _now_iso(),
    }
