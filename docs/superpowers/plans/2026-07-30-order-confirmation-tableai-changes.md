# Order Confirmation — TableAI Changes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the chatbot's post-payment landing page (`/order-complete`) know which order it's showing, and let it fetch enough detail (items, total) to render a recap — the two small pieces this repo owns for the order-confirmation feature.

**Architecture:** Two independent, small changes: (1) the existing order-status Lambda (`status_lambda/status_app.py`) starts returning `items` and `total` alongside the fields it already returns; (2) the chatbot's takeout fulfillment appends `?orderId={order_id}` to the Stripe Checkout `success_url` it already builds, so the browser lands on `/order-complete?orderId=TKOT-XXXXX` instead of a bare `/order-complete`.

**Tech Stack:** Python 3.13, pytest, Terraform (existing zip-Lambda deploy already in place — no new resources).

## Global Constraints

- Repo: `C:\Users\Ken\Desktop\TableAi\TableAI`, branch `feat/chatbot-ordering-backend` (the branch this session has been using for all chatbot backend work).
- The status endpoint still does **not** return `receiptEmail`, `phoneNumber`, or any payment/card details — only `orderId`, `paymentStatus`, `orderStatus`, `items`, and `total`. This is a deliberate, unchanged security boundary (order IDs are unauthenticated but not enumerable).
- No Terraform resource changes — this is a code-only change to an existing zip-packaged Lambda; `terraform apply` picks up the new code automatically via the existing `archive_file` data source's hash.
- Do not touch `precompute_embdeddings.py` (pre-existing unrelated modified file in this repo's working tree) or any other file outside what's listed below.

---

## Task 1: Extend the order-status Lambda's response with items and total

**Files:**
- Modify: `status_lambda/status_app.py`
- Modify: `status_lambda/test_status.py`

**Interfaces:**
- Produces: `lambda_handler` now returns `{"orderId", "paymentStatus", "orderStatus", "items", "total"}` on a 200. `items` and `total` are passed through as-is from the DynamoDB item (whatever shape/type they're already stored as); `total` in particular may come back as a `Decimal` from DynamoDB — the caller (Task 2's Terraform-deployed Lambda already `json.dumps`es the whole response dict, so `Decimal` needs the same treatment already used elsewhere in this codebase).

- [ ] **Step 1: Write the failing test**

Modify `status_lambda/test_status.py` — update `test_returns_status` to also assert the new fields, and add a new test for the `Decimal` case. Replace the file's content with:
```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd "C:\Users\Ken\Desktop\TableAi\TableAI\status_lambda" && py -3 -m pytest test_status.py -v`
Expected: FAIL — `KeyError: 'items'` (the new fields aren't in the response yet) and a `TypeError: Object of type Decimal is not JSON serializable` from `test_returns_status_with_items_and_total` (the current code has no `Decimal` handling).

- [ ] **Step 3: Update `status_app.py`**

Replace the entire contents of `status_lambda/status_app.py` with:
```python
import json
import os
from decimal import Decimal
import boto3

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ.get("ORDERS_TABLE_NAME", "momotaroOrdersDatabase"))

_CORS = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}


class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _resp(code, body):
    return {"statusCode": code, "headers": _CORS, "body": json.dumps(body, cls=_DecimalEncoder)}


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    order_id = params.get("orderId")
    if not order_id:
        return _resp(400, {"error": "orderId required"})
    item = table.get_item(Key={"orderId": order_id}).get("Item")
    if not item:
        return _resp(404, {"error": "not found"})
    return _resp(200, {
        "orderId": item.get("orderId"),
        "paymentStatus": item.get("paymentStatus"),
        "orderStatus": item.get("orderStatus"),
        "items": item.get("items", []),
        "total": item.get("total"),
    })
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd "C:\Users\Ken\Desktop\TableAi\TableAI\status_lambda" && py -3 -m pytest test_status.py -v`
Expected: PASS — 4 passed, output pristine.

- [ ] **Step 5: Deploy**

Run: `cd "C:\Users\Ken\Desktop\TableAi\TableAI\infrastructure" && terraform init -input=false && terraform apply -input=false -target=aws_lambda_function.order_status`
Review the plan (expect: `aws_lambda_function.order_status` updated in-place, code hash changed) and confirm.

- [ ] **Step 6: Verify against a real order**

Using a real recent order id: `curl "<order_status_url output>?orderId=<REAL-ORDER-ID>"` (the Function URL — get it via `terraform output order_status_url` if not already known). Expected: JSON response now includes populated `items` (a list) and `total` (a number), alongside the existing `orderId`/`paymentStatus`/`orderStatus`.

- [ ] **Step 7: Commit**

```bash
cd "C:\Users\Ken\Desktop\TableAi\TableAI"
git add status_lambda/status_app.py status_lambda/test_status.py
git commit -m "feat: order-status endpoint also returns items + total for the confirmation recap page"
```

---

## Task 2: Append the order id to the chatbot's Stripe success URL

**Files:**
- Modify: `lambda_container_project/app.py`

**Interfaces:**
- Produces: the takeout branch of `fulfill_order` now calls `create_checkout_session` with a success URL of `{STRIPE_SUCCESS_URL}?orderId={order_id}` instead of the bare `STRIPE_SUCCESS_URL`, so `/order-complete` receives `orderId` as a query parameter after the Stripe redirect.

- [ ] **Step 1: Write the failing test**

There is no existing unit test directly covering the exact URL string passed to `create_checkout_session` (the existing smoke tests in `test_fulfill_smoke.py` stub `create_checkout_session` with a lambda that ignores its arguments). Add one that captures and asserts on the call. In `lambda_container_project/test_fulfill_smoke.py`, add this test (it reuses the `FAKE_MENU_LOOKUP` and `_order_event` helpers already defined in that file):
```python
def test_takeout_success_url_includes_order_id(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, FAKE_MENU_LOOKUP, None))
    monkeypatch.setattr(app, "orders_table", MagicMock())
    monkeypatch.setattr(app, "sns_client", MagicMock())
    monkeypatch.setattr(app, "_stripe_key", lambda: "rk_live_dummy")

    captured = {}
    def _fake_create_checkout_session(order_id, order_items, total_cents, success_url, cancel_url, api_key):
        captured["order_id"] = order_id
        captured["success_url"] = success_url
        return "https://checkout.stripe.com/c/pay/cs_test_x"
    monkeypatch.setattr(app, "create_checkout_session", _fake_create_checkout_session)

    app.fulfill_order(_order_event("takeout"))

    assert captured["order_id"] in captured["success_url"]
    assert captured["success_url"].startswith(app.STRIPE_SUCCESS_URL)
    assert f"orderId={captured['order_id']}" in captured["success_url"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd "C:\Users\Ken\Desktop\TableAi\TableAI\lambda_container_project" && py -3 -m pytest test_fulfill_smoke.py::test_takeout_success_url_includes_order_id -v`
Expected: FAIL — `assert 'TKOT-...' in 'https://take-out.momotarosushi.ca/order-complete'` (the order id isn't in the URL yet).

- [ ] **Step 3: Update `fulfill_order`'s takeout branch**

In `lambda_container_project/app.py`, change:
```python
            url = create_checkout_session(
                order_id, order_items, total_cents,
                STRIPE_SUCCESS_URL, STRIPE_CANCEL_URL, _stripe_key())
```
to:
```python
            success_url = f"{STRIPE_SUCCESS_URL}?orderId={order_id}"
            url = create_checkout_session(
                order_id, order_items, total_cents,
                success_url, STRIPE_CANCEL_URL, _stripe_key())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd "C:\Users\Ken\Desktop\TableAi\TableAI\lambda_container_project" && py -3 -m pytest test_fulfill_smoke.py -v`
Expected: PASS — all smoke tests pass including the new one, output pristine.

- [ ] **Step 5: Run the full lambda_container_project suite**

Run: `cd "C:\Users\Ken\Desktop\TableAi\TableAI\lambda_container_project" && py -3 -m pytest -v`
Expected: all tests pass (ordering, payments, and smoke tests combined), output pristine.

- [ ] **Step 6: Commit**

```bash
cd "C:\Users\Ken\Desktop\TableAi\TableAI"
git add lambda_container_project/app.py lambda_container_project/test_fulfill_smoke.py
git commit -m "feat: include orderId in the chatbot's Stripe success URL for the confirmation page"
```

- [ ] **Step 7: Rebuild and redeploy the Lex fulfillment container**

Run (from repo root, matching the deploy pattern already used throughout this session):
```powershell
$acct="798965869505"; $region="ca-central-1"; $repo="momotaro-lex-bot"
aws ecr get-login-password --region $region | docker login --username AWS --password-stdin "$acct.dkr.ecr.$region.amazonaws.com"
docker build -t "$repo:latest" ./lambda_container_project
docker tag "$repo:latest" "$acct.dkr.ecr.$region.amazonaws.com/$repo:latest"
docker push "$acct.dkr.ecr.$region.amazonaws.com/$repo:latest"
aws lambda update-function-code --function-name TableAILexFulfillmentHandler --image-uri "$acct.dkr.ecr.$region.amazonaws.com/$repo:latest" --region $region
aws lambda wait function-updated --function-name TableAILexFulfillmentHandler --region $region
```

- [ ] **Step 8: Verify end-to-end via the prod bot alias**

Drive a takeout order through to the payment-link step (e.g. via `aws lexv2-runtime recognize-text` against bot alias `6JNOFO6XPY`, as used throughout this session) and confirm the returned Stripe Checkout URL, once paid, redirects to `https://take-out.momotarosushi.ca/order-complete?orderId=TKOT-XXXXX` with a real order id matching the one just placed.
