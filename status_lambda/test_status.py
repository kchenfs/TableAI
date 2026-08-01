import json, sys
from decimal import Decimal
from unittest.mock import MagicMock
sys.modules.setdefault("boto3", MagicMock())
import status_app

class _FakeTable:
    def __init__(self, item): self._item = item
    def get_item(self, Key): return {"Item": self._item} if self._item else {}

def test_returns_status_with_items_and_total(monkeypatch):
    monkeypatch.setattr(status_app, "table",
        _FakeTable({
            "orderId": "TKOT-ABCDE", "paymentStatus": "PAID", "orderStatus": "PENDING_KITCHEN",
            "items": [{"name": "Green Tea", "quantity": 1, "price": Decimal("2.00")}],
            "total": Decimal("2.26"),
        }))
    resp = status_app.lambda_handler({"queryStringParameters": {"orderId": "TKOT-ABCDE"}}, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["orderId"] == "TKOT-ABCDE"
    assert body["paymentStatus"] == "PAID"
    assert body["orderStatus"] == "PENDING_KITCHEN"
    assert body["items"] == [{"name": "Green Tea", "quantity": 1, "price": 2.00}]
    assert body["total"] == 2.26

def test_response_does_not_leak_receipt_email_or_phone(monkeypatch):
    monkeypatch.setattr(status_app, "table",
        _FakeTable({
            "orderId": "TKOT-ABCDE", "paymentStatus": "PAID", "orderStatus": "PENDING_KITCHEN",
            "items": [], "total": Decimal("2.26"),
            "receiptEmail": "customer@example.com", "phoneNumber": "+14165551234",
        }))
    resp = status_app.lambda_handler({"queryStringParameters": {"orderId": "TKOT-ABCDE"}}, None)
    body = json.loads(resp["body"])
    assert "receiptEmail" not in body
    assert "phoneNumber" not in body

def test_missing_order_404(monkeypatch):
    monkeypatch.setattr(status_app, "table", _FakeTable(None))
    resp = status_app.lambda_handler({"queryStringParameters": {"orderId": "TKOT-NOPE"}}, None)
    assert resp["statusCode"] == 404

def test_missing_param_400(monkeypatch):
    resp = status_app.lambda_handler({"queryStringParameters": None}, None)
    assert resp["statusCode"] == 400
