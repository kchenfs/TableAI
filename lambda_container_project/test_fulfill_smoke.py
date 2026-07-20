import json, importlib, os, sys, types
from unittest.mock import MagicMock
from decimal import Decimal

# Stub heavy deps so app.py imports without real AWS/AI setup.
os.environ.setdefault("MENU_TABLE_NAME", "m"); os.environ.setdefault("ORDERS_TABLE_NAME", "o")
for mod in ["openai", "google.generativeai", "numpy", "faiss", "boto3", "stripe"]:
    sys.modules.setdefault(mod.split(".")[0], types.ModuleType(mod.split(".")[0]))

# `import google.generativeai as genai` needs the submodule registered under
# sys.modules and set as an attribute of the parent package.
_google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
_generativeai_stub = sys.modules.setdefault("google.generativeai", types.ModuleType("google.generativeai"))
_google_pkg.generativeai = _generativeai_stub

# app.py needs `OpenAI` (a class it instantiates) from the openai stub, and
# `boto3.resource`/`boto3.client` (called at import time) to be callable.
sys.modules["openai"].OpenAI = lambda *a, **k: types.SimpleNamespace()
sys.modules["boto3"].resource = lambda *a, **k: types.SimpleNamespace(
    Table=lambda *a, **k: types.SimpleNamespace())
sys.modules["boto3"].client = lambda *a, **k: types.SimpleNamespace()

def test_no_items_returns_failed(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, {}, None))
    event = {"sessionState": {"intent": {"name": "OrderFood", "state": "ReadyForFulfillment"},
             "sessionAttributes": {"parsedOrder": json.dumps({"order_items": []}), "orderMode": "dine-in"}}}
    resp = app.fulfill_order(event)
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
    assert "what would you like" in resp["messages"][0]["content"].lower()


FAKE_MENU_LOOKUP = {
    "green tea": {
        "normalized_name": "green tea",
        "raw_item": {"ItemName": "Green Tea", "Price": "2.00",
                     "ItemNumber": 96, "Location": "front", "Options": []},
    }
}


def _order_event(mode, extra_attrs=None):
    sa = {
        "parsedOrder": json.dumps(
            {"order_items": [{"item_name": "green tea", "quantity": 1, "options": {}}]}),
        "orderMode": mode,
    }
    if extra_attrs:
        sa.update(extra_attrs)
    return {"sessionState": {
        "intent": {"name": "OrderFood", "state": "ReadyForFulfillment"},
        "sessionAttributes": sa}}


def test_takeout_writes_decimal_and_returns_url(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, FAKE_MENU_LOOKUP, None))
    monkeypatch.setattr(app, "orders_table", MagicMock())
    monkeypatch.setattr(app, "sns_client", MagicMock())
    monkeypatch.setattr(app, "create_checkout_session",
                        lambda *a, **k: "https://checkout.stripe.com/c/pay/cs_test_x")
    monkeypatch.setattr(app, "_stripe_key", lambda: "rk_live_dummy")

    resp = app.fulfill_order(_order_event("takeout"))

    # Stripe URL returned to the customer
    assert "checkout.stripe.com" in resp["messages"][0]["content"]
    # Order written to DynamoDB with Decimal (not float) money fields
    assert app.orders_table.put_item.called
    item = app.orders_table.put_item.call_args.kwargs["Item"]
    assert isinstance(item["total"], Decimal)
    # Session flags set for idempotency + frontend polling
    attrs = resp["sessionState"]["sessionAttributes"]
    assert attrs["orderPlaced"] == "true"
    assert attrs["pendingTakeoutOrderId"].startswith("TKOT-")


def test_dinein_publishes_sns_and_not_dynamo(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, FAKE_MENU_LOOKUP, None))
    monkeypatch.setattr(app, "orders_table", MagicMock())
    monkeypatch.setattr(app, "sns_client", MagicMock())

    resp = app.fulfill_order(_order_event("dine-in", {"tableId": "table-2"}))

    assert app.sns_client.publish.called
    assert not app.orders_table.put_item.called   # dine-in must NOT write to DynamoDB
    assert "kitchen" in resp["messages"][0]["content"].lower()
    assert resp["sessionState"]["sessionAttributes"]["orderPlaced"] == "true"


def test_dinein_missing_table_fails_closed(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, FAKE_MENU_LOOKUP, None))
    monkeypatch.setattr(app, "sns_client", MagicMock())
    monkeypatch.setattr(app, "orders_table", MagicMock())

    resp = app.fulfill_order(_order_event("dine-in"))  # no tableId

    assert not app.sns_client.publish.called
    assert not app.orders_table.put_item.called
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"


def test_idempotency_short_circuits(monkeypatch):
    import app
    monkeypatch.setattr(app, "orders_table", MagicMock())
    monkeypatch.setattr(app, "sns_client", MagicMock())

    resp = app.fulfill_order(
        _order_event("dine-in", {"tableId": "table-2", "orderPlaced": "true"}))

    assert not app.sns_client.publish.called
    assert not app.orders_table.put_item.called
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
