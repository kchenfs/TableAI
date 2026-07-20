import pytest
from decimal import Decimal
from ordering import resolve_and_price, UnknownMenuItem

# Minimal fake menu_lookup mirroring app.get_menu()'s structure.
MENU = {
    "green dragon roll": {
        "normalized_name": "green dragon roll",
        "raw_item": {
            "ItemName": "Green Dragon Roll", "Price": "12.00",
            "ItemNumber": 42, "Location": "back", "Options": [],
        },
    },
    "sashimi, sushi & maki combo": {
        "normalized_name": "sashimi, sushi & maki combo",
        "raw_item": {
            "ItemName": "Sashimi, Sushi & Maki Combo", "Price": "30.00",
            "ItemNumber": 7, "Location": "back",
            "Options": [
                {"name": "Combo Choice", "items": [
                    {"name": "A", "priceModifier": 0},
                    {"name": "B", "priceModifier": 5},
                ]},
            ],
        },
    },
}

def test_simple_item_price_and_shape():
    items, total = resolve_and_price(
        [{"item_name": "green dragon roll", "quantity": 2, "options": {}}], MENU)
    assert len(items) == 1
    it = items[0]
    assert it["name"] == "Green Dragon Roll"
    assert it["quantity"] == 2
    assert it["price"] == 12.00
    assert it["subtotal"] == 24.00
    assert it["id"] == 42
    assert it["location"] == "back"
    assert it["options"] == ""
    # 24.00 * 1.13 = 27.12 -> 2712 cents
    assert total == 2712

def test_option_price_modifier_applied():
    items, total = resolve_and_price(
        [{"item_name": "Sashimi, Sushi & Maki Combo", "quantity": 1,
          "options": {"Combo Choice": "B"}}], MENU)
    it = items[0]
    assert it["price"] == 35.00           # 30 + 5 modifier
    assert it["subtotal"] == 35.00
    assert it["options"] == "Combo Choice: B"
    # 35.00 * 1.13 = 39.55 -> 3955 cents
    assert total == 3955

def test_multiple_items_total_rounds_half_up():
    items, total = resolve_and_price([
        {"item_name": "green dragon roll", "quantity": 1, "options": {}},
        {"item_name": "green dragon roll", "quantity": 1, "options": {}},
    ], MENU)
    # subtotal 24.00 -> tax 27.12 -> 2712
    assert total == 2712
    assert len(items) == 2

def test_unknown_item_raises():
    with pytest.raises(UnknownMenuItem) as ei:
        resolve_and_price([{"item_name": "flying unicorn roll", "quantity": 1, "options": {}}], MENU)
    assert ei.value.item_name == "flying unicorn roll"

def test_non_numeric_quantity_fails_closed():
    with pytest.raises(UnknownMenuItem):
        resolve_and_price(
            [{"item_name": "green dragon roll", "quantity": "abc", "options": {}}], MENU)

def test_out_of_range_quantity_fails_closed():
    with pytest.raises(UnknownMenuItem):
        resolve_and_price(
            [{"item_name": "green dragon roll", "quantity": 0, "options": {}}], MENU)

from ordering import new_order_id, build_dinein_order, build_takeout_order

def test_new_order_id_format():
    oid = new_order_id("TKOT")
    assert oid.startswith("TKOT-")
    assert len(oid) == 10          # TKOT- + 5
    assert oid[5:].isalnum() and oid[5:].isupper()

def test_build_dinein_order_shape():
    items = [{"name": "Green Dragon Roll", "quantity": 1, "price": 12.0,
              "subtotal": 12.0, "options": "", "id": 42, "location": "back"}]
    o = build_dinein_order("DINE-ABCDE", "table-2", items, 1356, "no wasabi")
    assert o["orderId"] == "DINE-ABCDE"
    assert o["orderType"] == "dine-in"
    assert o["orderStatus"] == "PENDING_KITCHEN"
    assert o["paymentStatus"] == "Dine-In"
    assert o["tableId"] == "table-2"
    assert o["items"] == items
    assert o["notes"] == "no wasabi"
    assert o["total"] == 13.56
    assert "orderDate" in o

def test_build_takeout_order_shape():
    items = [{"name": "Green Dragon Roll", "quantity": 1, "price": 12.0,
              "subtotal": 12.0, "options": "", "id": 42, "location": "back"}]
    o = build_takeout_order("TKOT-ABCDE", items, 1356, "")
    assert o["orderId"] == "TKOT-ABCDE"
    assert o["orderType"] == "takeout"
    assert o["orderStatus"] == "PENDING_PAYMENT"
    assert o["paymentStatus"] == "UNPAID"
    assert o["total"] == 13.56
    assert "tableId" not in o
