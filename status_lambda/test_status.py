import json, sys
from unittest.mock import MagicMock
sys.modules.setdefault("boto3", MagicMock())
import status_app

class _FakeTable:
    def __init__(self, item): self._item = item
    def get_item(self, Key): return {"Item": self._item} if self._item else {}

def test_returns_status(monkeypatch):
    monkeypatch.setattr(status_app, "table",
        _FakeTable({"orderId": "TKOT-ABCDE", "paymentStatus": "PAID", "orderStatus": "PENDING_KITCHEN"}))
    resp = status_app.lambda_handler({"queryStringParameters": {"orderId": "TKOT-ABCDE"}}, None)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body == {"orderId": "TKOT-ABCDE", "paymentStatus": "PAID", "orderStatus": "PENDING_KITCHEN"}

def test_missing_order_404(monkeypatch):
    monkeypatch.setattr(status_app, "table", _FakeTable(None))
    resp = status_app.lambda_handler({"queryStringParameters": {"orderId": "TKOT-NOPE"}}, None)
    assert resp["statusCode"] == 404

def test_missing_param_400(monkeypatch):
    resp = status_app.lambda_handler({"queryStringParameters": None}, None)
    assert resp["statusCode"] == 400
