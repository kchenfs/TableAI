import pytest
from decimal import Decimal
from ordering import resolve_and_price, UnknownMenuItem

# Minimal fake menu_lookup mirroring app.get_menu()'s structure.
MENU = {
    "green dragon roll": {
        "normalized_name": "green dragon roll",
        "raw_item": {
            "ItemName": "Green Dragon Roll", "Price": "12.00",
            # ItemNumber is a Decimal in production (boto3 DynamoDB), not a plain int.
            "ItemNumber": Decimal("42"), "Location": "back", "Options": [],
        },
    },
    "sashimi, sushi & maki combo": {
        "normalized_name": "sashimi, sushi & maki combo",
        "raw_item": {
            "ItemName": "Sashimi, Sushi & Maki Combo", "Price": "30.00",
            "ItemNumber": Decimal("7"), "Location": "back",
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
    assert it["id"] == "42"          # coerced from Decimal to a JSON-safe string
    assert it["location"] == "back"
    assert it["options"] == ""
    # 24.00 * 1.13 = 27.12 -> 2712 cents
    assert total == 2712

def test_order_items_are_json_serializable_with_decimal_item_numbers():
    # Regression: DynamoDB returns ItemNumber as Decimal and the order dict is
    # later json.dumps'd for SNS/put_item — the id must not leak a Decimal.
    import json
    items, _ = resolve_and_price(
        [{"item_name": "green dragon roll", "quantity": 1, "options": {}}], MENU)
    assert items[0]["id"] == "42"
    json.dumps(items)  # must not raise "Object of type Decimal is not JSON serializable"

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

from ordering import new_order_id, build_dinein_order, build_takeout_order, sanitize_customer_name

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
    assert "customerName" not in o          # absent-means-unset when no name given


def test_build_takeout_order_includes_customer_name_when_given():
    o = build_takeout_order("TKOT-ABCDE", [], 1356, "", customer_name="Ken")
    assert o["customerName"] == "Ken"


def test_build_takeout_order_omits_empty_customer_name():
    # A falsy name (None or "") must not be stored — keeps the Stripe fallback
    # able to fire downstream.
    assert "customerName" not in build_takeout_order("TKOT-A", [], 1356, "", customer_name=None)
    assert "customerName" not in build_takeout_order("TKOT-B", [], 1356, "", customer_name="")


def test_sanitize_customer_name():
    assert sanitize_customer_name("Ken") == "Ken"
    assert sanitize_customer_name(None) is None
    assert sanitize_customer_name("") is None
    assert sanitize_customer_name("   \t ") is None
    assert sanitize_customer_name("  Ken   Chen \n") == "Ken Chen"
    assert sanitize_customer_name("Ke\x07n\x00\x1b") == "Ken"
    assert sanitize_customer_name("A" * 200) == "A" * 40
    assert sanitize_customer_name(12345) == "12345"

# --- per-channel pricing -------------------------------------------------

DINE_IN_MENU = {
    "bento box": {
        "normalized_name": "bento box",
        "raw_item": {
            "ItemName": "Bento Box", "Price": "15.99", "DineInPrice": "14.99",
            "ItemNumber": Decimal("96"), "Location": "back",
            "Options": [
                {"name": "Select Bento Box", "items": [
                    {"name": "Beef Short Rib Bento Box",
                     "priceModifier": 2, "dineInPriceModifier": 1},
                    {"name": "Tempura Bento Box", "priceModifier": 0},
                ]},
            ],
        },
    },
    "green tea": {
        "normalized_name": "green tea",
        "raw_item": {
            # No DineInPrice - the "same price in both channels" case.
            "ItemName": "Green Tea", "Price": "2.00",
            "ItemNumber": Decimal("200"), "Location": "front", "Options": [],
        },
    },
}


def test_takeout_mode_uses_takeout_price():
    items, _ = resolve_and_price(
        [{"item_name": "bento box", "quantity": 1, "options": {}}],
        DINE_IN_MENU, "takeout")
    assert items[0]["price"] == 15.99


def test_dine_in_mode_uses_dine_in_price():
    items, _ = resolve_and_price(
        [{"item_name": "bento box", "quantity": 1, "options": {}}],
        DINE_IN_MENU, "dine-in")
    assert items[0]["price"] == 14.99


def test_dine_in_falls_back_to_price_when_no_dine_in_price():
    items, _ = resolve_and_price(
        [{"item_name": "green tea", "quantity": 1, "options": {}}],
        DINE_IN_MENU, "dine-in")
    assert items[0]["price"] == 2.00


def test_defaults_to_takeout_when_mode_omitted():
    items, _ = resolve_and_price(
        [{"item_name": "bento box", "quantity": 1, "options": {}}], DINE_IN_MENU)
    assert items[0]["price"] == 15.99


def test_option_modifier_resolves_per_channel():
    line = [{"item_name": "bento box", "quantity": 1,
             "options": {"Select Bento Box": "Beef Short Rib Bento Box"}}]
    takeout, _ = resolve_and_price(line, DINE_IN_MENU, "takeout")
    dine_in, _ = resolve_and_price(line, DINE_IN_MENU, "dine-in")
    assert takeout[0]["price"] == 17.99   # 15.99 + 2
    assert dine_in[0]["price"] == 15.99   # 14.99 + 1


def test_option_modifier_falls_back_when_no_dine_in_modifier():
    line = [{"item_name": "bento box", "quantity": 1,
             "options": {"Select Bento Box": "Tempura Bento Box"}}]
    dine_in, _ = resolve_and_price(line, DINE_IN_MENU, "dine-in")
    assert dine_in[0]["price"] == 14.99   # 14.99 + 0
