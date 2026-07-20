# Chatbot Ordering — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Lex fulfillment Lambda actually place chat orders — dine-in straight to the kitchen (via the existing SNS `order_events` topic), and takeout by writing a `PENDING_PAYMENT` order plus a Stripe Checkout Session link that the existing `TakeoutIngress` webhook fulfills — plus a tiny status endpoint the frontend will poll.

**Architecture:** All new order-building/pricing/payment logic lives in pure, unit-tested modules (`ordering.py`, `payments.py`); `app.py`'s `fulfill_order()` becomes thin wiring that calls them and branches on an `orderMode` session attribute. The Lambda is a container image deployed via Terraform in `infrastructure/`. Downstream (`TakeoutIngress`, `KitchenDisplayPublisher`, `DineInOrderWriter`, `TakeoutPaymentUpdater`, the printer) is untouched.

**Tech Stack:** Python 3.13 (container Lambda), `stripe` SDK, boto3 (DynamoDB, SNS, SSM), Terraform, pytest.

## Global Constraints

- Repo root: `C:\Users\Ken\Desktop\TableAi\TableAI`. Lambda source: `lambda_container_project/`. Terraform: `infrastructure/`.
- The order **total is always computed server-side** from `MomotaroSushiMenu_DB` (base `Price` + matched option `priceModifier`), tax rate **0.13**, rounded **half-up** to whole cents. Never trust a price from the chat/LLM.
- Menu item price field is a **string** in DynamoDB (`raw_item['Price']`, e.g. `"12.00"`); options live at `raw_item['Options']` = list of `{name, items:[{name, priceModifier}]}`.
- The parsed chat order (session attribute `parsedOrder`) is JSON: `{"order_items": [{"item_name": str, "quantity": int, "options": {group: choice}}]}`.
- Order the fanout consumes must carry (website shape, from the live dine-in record): `orderId`, `orderType` (`dine-in`|`takeout`), `orderStatus`, `paymentStatus`, `tableId` (dine-in), `items` (list of `{name, quantity, price, subtotal, options, id, location}` where `options` is a **string** `"Group: Choice; Group2: Choice2"`), `notes`, `total` (float dollars), `orderDate` (ISO, America/Toronto).
- SNS topic ARN: `arn:aws:sns:ca-central-1:798965869505:MomotaroFanoutStack-MomotaroOrderEventsTopic94459F09-vMCrg59Bej1p` (passed via env var `SNS_TOPIC_ARN`).
- Stripe secret: SSM SecureString `/momotaro/prod/STRIPE_SECRET_KEY` (read at cold start).
- Stripe Checkout Session MUST set `payment_intent_data={'metadata': {'order_id': <TKOT-id>}}` so the existing `TakeoutIngress` webhook (which reads `payment_intent.metadata.order_id`) works unchanged.
- Order IDs: `DINE-<5 uppercase alnum>` / `TKOT-<5 uppercase alnum>`.

---

## Task 1: Add the `stripe` dependency to the container

**Files:**
- Modify: `lambda_container_project/requirements.txt`

**Interfaces:**
- Produces: the `stripe` package available for import in tasks 3+.

- [ ] **Step 1: Add stripe to requirements.txt**

The current file is:
```
openai
google-generativeai
numpy
faiss-cpu
```
Change it to (append one line):
```
openai
google-generativeai
numpy
faiss-cpu
stripe
```

- [ ] **Step 2: Verify it installs locally (sanity, not the Lambda build)**

Run: `python -m pip install stripe==11.* --quiet && python -c "import stripe; print(stripe.__version__)"`
Expected: prints a version like `11.x.x` with no error.

- [ ] **Step 3: Commit**

```bash
git add lambda_container_project/requirements.txt
git commit -m "build: add stripe SDK to the Lex fulfillment container"
```

---

## Task 2: Pricing + item resolution (`ordering.py`)

**Files:**
- Create: `lambda_container_project/ordering.py`
- Test: `lambda_container_project/test_ordering.py`

**Interfaces:**
- Produces:
  - `resolve_and_price(parsed_items: list[dict], menu_lookup: dict) -> tuple[list[dict], int]` — returns `(order_items, total_cents)`. `order_items` are website-shaped dicts `{name, quantity, price, subtotal, options, id, location}` (`price`/`subtotal` are floats, `options` a string). Raises `UnknownMenuItem(item_name)` if a parsed item can't be resolved.
  - `class UnknownMenuItem(Exception)` with `.item_name`.
  - `TAX_RATE = Decimal("0.13")`
- Consumes: `menu_lookup` shape from `app.get_menu()` — `menu_lookup[normalized_name] = {"raw_item": {...}, "normalized_name": str, "options": {...}, "price": str, ...}`. `raw_item` holds `ItemName`, `Price` (str), `ItemNumber`, `Location`, `Options` (list of `{name, items:[{name, priceModifier}]}`).

- [ ] **Step 1: Write the failing tests**

Create `lambda_container_project/test_ordering.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd lambda_container_project && python -m pytest test_ordering.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ordering'`.

- [ ] **Step 3: Write the implementation**

Create `lambda_container_project/ordering.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd lambda_container_project && python -m pytest test_ordering.py -v`
Expected: PASS — 4 passed.

- [ ] **Step 5: Commit**

```bash
git add lambda_container_project/ordering.py lambda_container_project/test_ordering.py
git commit -m "feat: server-side pricing + item resolution for chat orders"
```

---

## Task 3: Order-object builders + id generator (`ordering.py`)

**Files:**
- Modify: `lambda_container_project/ordering.py`
- Modify: `lambda_container_project/test_ordering.py`

**Interfaces:**
- Produces (in `ordering.py`):
  - `new_order_id(prefix: str) -> str` — `f"{prefix}-{5 uppercase alnum}"`.
  - `build_dinein_order(order_id, table_id, items, total_cents, notes) -> dict`
  - `build_takeout_order(order_id, items, total_cents, notes) -> dict`
  - Both stamp `orderDate` as ISO in America/Toronto and set `total` = dollars (`total_cents/100`).

- [ ] **Step 1: Write the failing tests (append to test_ordering.py)**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd lambda_container_project && python -m pytest test_ordering.py -v`
Expected: FAIL — `ImportError: cannot import name 'new_order_id'`.

- [ ] **Step 3: Add the implementation to `ordering.py`**

Append to `lambda_container_project/ordering.py`:
```python
import random
import string
from datetime import datetime
from zoneinfo import ZoneInfo


def new_order_id(prefix):
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"{prefix}-{suffix}"


def _now_iso():
    return datetime.now(ZoneInfo("America/Toronto")).isoformat()


def build_dinein_order(order_id, table_id, items, total_cents, notes):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd lambda_container_project && python -m pytest test_ordering.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 5: Commit**

```bash
git add lambda_container_project/ordering.py lambda_container_project/test_ordering.py
git commit -m "feat: dine-in/takeout order builders + order-id generator"
```

---

## Task 4: Stripe Checkout Session helper (`payments.py`)

**Files:**
- Create: `lambda_container_project/payments.py`
- Test: `lambda_container_project/test_payments.py`

**Interfaces:**
- Produces: `create_checkout_session(order_id, order_items, total_cents, success_url, cancel_url, api_key) -> str` — creates a Stripe Checkout Session (mode=`payment`, single CAD line item for the tax-inclusive total, `payment_intent_data.metadata.order_id=order_id`) and returns `session.url`.
- Consumes: `order_items` from Task 2 (only used for the line-item name/description).

- [ ] **Step 1: Write the failing test (with a fake stripe module injected)**

Create `lambda_container_project/test_payments.py`:
```python
import types
import payments

class _FakeSession:
    url = "https://checkout.stripe.com/c/pay/cs_test_123"

def test_create_checkout_session_builds_correct_params(monkeypatch):
    captured = {}
    def fake_create(**kwargs):
        captured.update(kwargs)
        return _FakeSession()
    fake_stripe = types.SimpleNamespace(
        checkout=types.SimpleNamespace(
            Session=types.SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(payments, "stripe", fake_stripe)

    url = payments.create_checkout_session(
        order_id="TKOT-ABCDE",
        order_items=[{"name": "Green Dragon Roll", "quantity": 2}],
        total_cents=2712,
        success_url="https://take-out.momotarosushi.ca/order-complete",
        cancel_url="https://take-out.momotarosushi.ca/",
        api_key="rk_live_dummy",
    )
    assert url == "https://checkout.stripe.com/c/pay/cs_test_123"
    assert captured["mode"] == "payment"
    assert captured["payment_intent_data"]["metadata"]["order_id"] == "TKOT-ABCDE"
    li = captured["line_items"][0]
    assert li["price_data"]["currency"] == "cad"
    assert li["price_data"]["unit_amount"] == 2712
    assert li["quantity"] == 1
    assert captured["success_url"] == "https://take-out.momotarosushi.ca/order-complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lambda_container_project && python -m pytest test_payments.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'payments'`.

- [ ] **Step 3: Write the implementation**

Create `lambda_container_project/payments.py`:
```python
"""Stripe Checkout Session creation for chat takeout orders."""
import stripe


def create_checkout_session(order_id, order_items, total_cents,
                            success_url, cancel_url, api_key):
    stripe.api_key = api_key
    # One combined line for the tax-inclusive total; itemized names in the description.
    description = ", ".join(
        f'{it.get("quantity", 1)}x {it.get("name", "item")}' for it in order_items)
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "cad",
                "unit_amount": total_cents,
                "product_data": {
                    "name": "Momotaro Takeout Order",
                    "description": description or order_id,
                },
            },
            "quantity": 1,
        }],
        payment_intent_data={"metadata": {"order_id": order_id}},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return session.url
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd lambda_container_project && python -m pytest test_payments.py -v`
Expected: PASS — 1 passed.

- [ ] **Step 5: Commit**

```bash
git add lambda_container_project/payments.py lambda_container_project/test_payments.py
git commit -m "feat: Stripe Checkout Session helper for chat takeout"
```

---

## Task 5: Wire `fulfill_order()` in `app.py`

**Files:**
- Modify: `lambda_container_project/app.py` (imports near top; `fulfill_order` at lines 562-584)

**Interfaces:**
- Consumes: `resolve_and_price`, `new_order_id`, `build_dinein_order`, `build_takeout_order`, `UnknownMenuItem` (Task 2/3); `create_checkout_session` (Task 4); `get_menu`, `close_dialog`, `orders_table` (existing in `app.py`).

- [ ] **Step 1: Add module-level clients/config near the top of `app.py`**

Immediately after the existing client setup block (after line 39, the `client = OpenAI(...)` block), add. (These imports are self-contained — do not assume `traceback`/`decimal`/a `DecimalEncoder` already exist in `app.py`; this block brings its own.)
```python
import traceback as _traceback
import boto3 as _boto3
from decimal import Decimal as _Decimal
from ordering import (resolve_and_price, new_order_id, build_dinein_order,
                      build_takeout_order, UnknownMenuItem)
from payments import create_checkout_session

sns_client = _boto3.client("sns")
_ssm = _boto3.client("ssm")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "https://take-out.momotarosushi.ca/order-complete")
STRIPE_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL", "https://take-out.momotarosushi.ca/")
_STRIPE_KEY = None

def _stripe_key():
    global _STRIPE_KEY
    if _STRIPE_KEY is None:
        _STRIPE_KEY = _ssm.get_parameter(
            Name="/momotaro/prod/STRIPE_SECRET_KEY", WithDecryption=True)["Parameter"]["Value"]
    return _STRIPE_KEY
```

- [ ] **Step 2: Replace the body of `fulfill_order` (lines 562-584)**

Replace the entire existing `fulfill_order` function with:
```python
def fulfill_order(event, allergy_info=None):
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}

    # Idempotency: once an order is placed in this session, don't place another.
    if session_attrs.get('orderPlaced') == 'true':
        return close_dialog(event, session_attrs, 'Fulfilled',
            {'contentType': 'PlainText', 'content': "Your order is already in — anything else?"})

    try:
        parsed = json.loads(session_attrs.get('parsedOrder', '{}'))
        parsed_items = parsed.get('order_items', [])
        if not parsed_items:
            return close_dialog(event, session_attrs, 'Failed',
                {'contentType': 'PlainText', 'content': "I didn't catch any items — what would you like?"})

        _, menu_lookup, _ = get_menu()
        try:
            order_items, total_cents = resolve_and_price(parsed_items, menu_lookup)
        except UnknownMenuItem as e:
            return close_dialog(event, session_attrs, 'Failed',
                {'contentType': 'PlainText',
                 'content': f"Sorry, I couldn't find \"{e.item_name}\" on the menu. Could you rephrase it?"})

        notes = session_attrs.get('orderNotes', '')
        mode = session_attrs.get('orderMode', 'dine-in')

        if mode == 'takeout':
            order_id = new_order_id('TKOT')
            order = build_takeout_order(order_id, order_items, total_cents, notes)
            # DynamoDB rejects floats — convert them to Decimal on the way in.
            orders_table.put_item(Item=json.loads(json.dumps(order), parse_float=_Decimal))
            url = create_checkout_session(
                order_id, order_items, total_cents,
                STRIPE_SUCCESS_URL, STRIPE_CANCEL_URL, _stripe_key())
            session_attrs['orderPlaced'] = 'true'
            session_attrs['pendingTakeoutOrderId'] = order_id
            msg = f"Your total is ${total_cents/100:.2f}. Pay securely here to confirm: {url}"
            return close_dialog(event, session_attrs, 'Fulfilled',
                {'contentType': 'PlainText', 'content': msg})

        # dine-in — the order dict holds only str/int/float, so plain json.dumps is safe.
        table_id = session_attrs.get('tableId')
        if not table_id:
            return close_dialog(event, session_attrs, 'Failed',
                {'contentType': 'PlainText',
                 'content': "I couldn't tell which table you're at — please reopen the menu from your table's QR code and try again."})
        order_id = new_order_id('DINE')
        order = build_dinein_order(order_id, table_id, order_items, total_cents, notes)
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps(order),
            MessageAttributes={'orderType': {'DataType': 'String', 'StringValue': 'dine-in'}})
        session_attrs['orderPlaced'] = 'true'
        return close_dialog(event, session_attrs, 'Fulfilled',
            {'contentType': 'PlainText',
             'content': f"Order placed — ${total_cents/100:.2f}, sent to the kitchen! (Table {table_id})"})

    except Exception as e:
        print(f"Error fulfilling order: {e}"); _traceback.print_exc()
        return close_dialog(event, session_attrs, 'Failed',
            {'contentType': 'PlainText', 'content': "I hit an error finalizing your order. Please try again."})
```

- [ ] **Step 3: Syntax-check the module**

Run: `cd lambda_container_project && python -c "import ast; ast.parse(open('app.py', encoding='utf-8').read()); print('app.py syntax OK')"`
Expected: `app.py syntax OK`.

- [ ] **Step 4: Smoke-test the branching with a crafted Lex event (no AWS calls needed for the takeout-missing-items and unknown-item paths)**

Create `lambda_container_project/test_fulfill_smoke.py`:
```python
import json, importlib, os, sys, types

# Stub heavy deps so app.py imports without real AWS/AI setup.
os.environ.setdefault("MENU_TABLE_NAME", "m"); os.environ.setdefault("ORDERS_TABLE_NAME", "o")
for mod in ["openai", "google.generativeai", "numpy", "faiss", "boto3", "stripe"]:
    sys.modules.setdefault(mod.split(".")[0], types.ModuleType(mod.split(".")[0]))

def test_no_items_returns_failed(monkeypatch):
    import app
    monkeypatch.setattr(app, "get_menu", lambda *a, **k: (None, {}, None))
    event = {"sessionState": {"intent": {"name": "OrderFood", "state": "ReadyForFulfillment"},
             "sessionAttributes": {"parsedOrder": json.dumps({"order_items": []}), "orderMode": "dine-in"}}}
    resp = app.fulfill_order(event)
    assert resp["sessionState"]["dialogAction"]["type"] == "Close"
    assert "what would you like" in resp["messages"][0]["content"].lower()
```

Run: `cd lambda_container_project && python -m pytest test_fulfill_smoke.py -v`
Expected: PASS — 1 passed. (If import of `app` fails on a missing stub, add that module name to the stub list — do not add real deps.)

- [ ] **Step 5: Commit**

```bash
git add lambda_container_project/app.py lambda_container_project/test_fulfill_smoke.py
git commit -m "feat: fulfill_order places real dine-in/takeout chat orders"
```

---

## Task 6: Terraform — IAM (SNS/SSM/KMS) + env vars for the Lex Lambda

**Files:**
- Modify: `infrastructure/lambda.tf`
- Modify: `infrastructure/variables.tf`

**Interfaces:**
- Produces: the deployed Lambda gets `SNS_TOPIC_ARN`, `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL` env vars and IAM for `sns:Publish`, `ssm:GetParameter`, `kms:Decrypt`.

- [ ] **Step 1: Add variables**

Append to `infrastructure/variables.tf`:
```hcl
variable "order_events_topic_arn" {
  description = "SNS order_events topic from the Fanout stack"
  type        = string
  default     = "arn:aws:sns:ca-central-1:798965869505:MomotaroFanoutStack-MomotaroOrderEventsTopic94459F09-vMCrg59Bej1p"
}

variable "stripe_secret_param" {
  description = "SSM SecureString name for the Stripe secret key"
  type        = string
  default     = "/momotaro/prod/STRIPE_SECRET_KEY"
}
```

- [ ] **Step 2: Add IAM statements to `lex_fulfillment_policy`**

In `infrastructure/lambda.tf`, inside `aws_iam_policy.lex_fulfillment_policy`'s `Statement = [ ... ]`, add these three statements (after the DynamoDB statement, before the ECR ones):
```hcl
      {
        Sid      = "PublishOrderEvents"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = var.order_events_topic_arn
      },
      {
        Sid      = "ReadStripeSecret"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter${var.stripe_secret_param}"
      },
      {
        Sid      = "DecryptStripeSecretViaSsm"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com" }
        }
      },
```

- [ ] **Step 3: Add env vars to the Lambda**

In `aws_lambda_function.lex_fulfillment_handler`'s `environment.variables` block, add:
```hcl
      SNS_TOPIC_ARN      = var.order_events_topic_arn
      STRIPE_SUCCESS_URL = "https://take-out.momotarosushi.ca/order-complete"
      STRIPE_CANCEL_URL  = "https://take-out.momotarosushi.ca/"
```

- [ ] **Step 4: Validate the Terraform**

Run: `cd infrastructure && terraform init -input=false && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

```bash
git add infrastructure/lambda.tf infrastructure/variables.tf
git commit -m "infra: grant Lex Lambda SNS publish + SSM/KMS for Stripe, add env vars"
```

---

## Task 7: Order-status endpoint (`status_lambda/` + Terraform Function URL)

**Files:**
- Create: `status_lambda/status_app.py`
- Create: `status_lambda/test_status.py`
- Modify: `infrastructure/status.tf` (create new file)

**Interfaces:**
- Produces: `GET <function-url>?orderId=TKOT-…` → `200 {"orderId","paymentStatus","orderStatus"}` or `404 {"error":"not found"}`. CORS enabled for browser polling.
- Produces (Python): `status_app.lambda_handler(event, context)` reading `event["queryStringParameters"]["orderId"]`.

- [ ] **Step 1: Write the failing test**

Create `status_lambda/test_status.py`:
```python
import json, sys
from unittest.mock import MagicMock
sys.modules.setdefault("boto3", MagicMock())  # so `boto3.resource(...)` at import works
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd status_lambda && python -m pytest test_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'status_app'`.

- [ ] **Step 3: Write the implementation**

Create `status_lambda/status_app.py`:
```python
import json
import os
import boto3

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ.get("ORDERS_TABLE_NAME", "momotaroOrdersDatabase"))

_CORS = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}


def _resp(code, body):
    return {"statusCode": code, "headers": _CORS, "body": json.dumps(body)}


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
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd status_lambda && python -m pytest test_status.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 5: Add Terraform for the status Lambda + Function URL**

Create `infrastructure/status.tf`:
```hcl
# Order-status endpoint: a zip Lambda + public Function URL the chat page polls.
data "archive_file" "status_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../status_lambda"
  output_path = "${path.module}/status_lambda.zip"
  excludes    = ["test_status.py"]
}

resource "aws_iam_role" "order_status_role" {
  name = "OrderStatusRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow",
      Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "order_status_policy" {
  name = "OrderStatusPolicy"
  role = aws_iam_role.order_status_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" },
      { Effect = "Allow", Action = "dynamodb:GetItem", Resource = data.aws_dynamodb_table.orders.arn },
    ]
  })
}

resource "aws_lambda_function" "order_status" {
  function_name    = "OrderStatus"
  role             = aws_iam_role.order_status_role.arn
  runtime          = "python3.13"
  handler          = "status_app.lambda_handler"
  filename         = data.archive_file.status_zip.output_path
  source_code_hash = data.archive_file.status_zip.output_base64sha256
  timeout          = 10
  environment { variables = { ORDERS_TABLE_NAME = data.aws_dynamodb_table.orders.name } }
}

resource "aws_lambda_function_url" "order_status_url" {
  function_name      = aws_lambda_function.order_status.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    allow_methods = ["GET"]
  }
}

output "order_status_url" {
  value = aws_lambda_function_url.order_status_url.function_url
}
```

- [ ] **Step 6: Validate Terraform**

Run: `cd infrastructure && terraform init -input=false && terraform validate`
Expected: `Success! The configuration is valid.`
(If `archive` provider is missing, run `terraform init -upgrade`.)

- [ ] **Step 7: Commit**

```bash
git add status_lambda/status_app.py status_lambda/test_status.py infrastructure/status.tf
git commit -m "feat: order-status polling endpoint (Lambda + Function URL)"
```

---

## Task 8: Build, deploy, and verify end-to-end

**Files:** none (build/deploy/verify only).

- [ ] **Step 1: Run the full unit suite**

Run: `cd lambda_container_project && python -m pytest -v && cd ../status_lambda && python -m pytest -v`
Expected: all tests pass, output pristine.

- [ ] **Step 2: Build + push the container image**

Run (PowerShell, from repo root):
```powershell
$acct="798965869505"; $region="ca-central-1"; $repo="momotaro-lex-bot"
aws ecr get-login-password --region $region | docker login --username AWS --password-stdin "$acct.dkr.ecr.$region.amazonaws.com"
docker build -t "$repo:latest" ./lambda_container_project
docker tag "$repo:latest" "$acct.dkr.ecr.$region.amazonaws.com/$repo:latest"
docker push "$acct.dkr.ecr.$region.amazonaws.com/$repo:latest"
```
Expected: image pushes successfully.

- [ ] **Step 3: Apply Terraform**

Run: `cd infrastructure && terraform apply` — review the plan (expect: IAM policy update, Lambda env-var update, new OrderStatus Lambda + Function URL). Confirm `yes`. Note the printed `order_status_url` output.

- [ ] **Step 4: Point the running Lex Lambda at the new image**

Run (PowerShell): `aws lambda update-function-code --function-name TableAILexFulfillmentHandler --image-uri "798965869505.dkr.ecr.ca-central-1.amazonaws.com/momotaro-lex-bot:latest" --region ca-central-1` then `aws lambda wait function-updated --function-name TableAILexFulfillmentHandler --region ca-central-1`.

- [ ] **Step 5: Verify dine-in end-to-end (Lex test console)**

In the Lex V2 console → `TableAIOrderBot` → Test, set session attributes `orderMode=dine-in`, `tableId=table-2`, then order (e.g. "two green dragon rolls"). Expect the bot to reply "Order placed — $… sent to the kitchen! (Table table-2)". Then confirm: a new `DINE-…` order appears in `momotaroOrdersDatabase` (written by `DineInOrderWriter`), and it printed / showed on the kitchen display (or check `KitchenDisplayPublisher` logs: "Routing order DINE-… to kitchen display").

- [ ] **Step 6: Verify takeout end-to-end**

Set `orderMode=takeout` in the test session and order. Expect a reply with a `https://checkout.stripe.com/...` link and the correct total. Confirm a `TKOT-…` order exists in DynamoDB as `PENDING_PAYMENT/UNPAID`. Open the link, pay with a live card (small real order — you can refund it), and confirm: the Stripe `payment_intent.succeeded` webhook → `TakeoutIngress` flips the order to `PAID` and it prints. Then `curl "<order_status_url>?orderId=TKOT-…"` returns `{"paymentStatus":"PAID",...}`.

- [ ] **Step 7: Commit a note (no code)**

```bash
git commit --allow-empty -m "chore: chatbot ordering backend deployed and verified end-to-end"
```

---

## Post-implementation

The backend now lets the bot place real orders. The **frontend plan** (separate) adds: passing `orderMode`/`tableId` session attributes from the site, polling `order_status_url` after a takeout order, and injecting the auto-appear confirmation card (address + Get Directions) into the lex-web-ui widget. It consumes `pendingTakeoutOrderId` (session attribute) and `order_status_url` (Terraform output) from this plan.
