# Chatbot Ordering (Lex → Kitchen) — Design

## Problem

The Momotaro Lex chatbot (`TableAIOrderBot`, fulfilled by the container Lambda
`TableAILexFulfillmentHandler`, source in `lambda_container_project/app.py`)
already does the hard AI work: LLM intent classification, RAG menu Q&A
(`knowledge_base.json`), allergy handling, and parsing free-text into
structured order items (via OpenRouter). But the final step, `fulfill_order()`,
only replies *"Thank you! Your order has been placed."* and **does nothing
else** — it never writes to the orders table, never routes to the kitchen, and
never takes payment. So a chat "order" evaporates: nothing prints, no one pays.

## Goal

Make chat orders real, for **both dine-in and takeout**, by feeding them into
the **exact same pipeline the website cart already uses** — the SNS
`order_events` topic → `KitchenDisplayPublisher` → printer/display, and the
Stripe → `TakeoutIngress` webhook path for takeout. The chatbot becomes just
another producer into that pipeline; the downstream functions are untouched.

## Current infrastructure (reused as-is)

- **SNS order-events topic:** `arn:aws:sns:ca-central-1:798965869505:MomotaroFanoutStack-MomotaroOrderEventsTopic94459F09-vMCrg59Bej1p`
  - Subscribers: `DineInOrderWriter` (writes order to DB), `KitchenDisplayPublisher`
    (publishes to IoT `printers/orders/print` → printer + display),
    `TakeoutPaymentUpdater` (marks takeout PAID), `ReceiptEmailSender` (emails receipt).
- **Orders table:** `momotaroOrdersDatabase` (partition key `orderId`).
- **Menu table:** `MomotaroSushiMenu_DB` (already read by the fulfillment Lambda).
- **Stripe secret:** SSM SecureString `/momotaro/prod/STRIPE_SECRET_KEY` (live `rk_live_`).
- **Takeout webhook:** `TakeoutIngress` (`yr3f71ush7`) — verifies `payment_intent.succeeded`
  by signature, looks up the order by `metadata.order_id`, publishes to the SNS topic.
- **Lex fulfillment Lambda:** `TableAILexFulfillmentHandler` (container image
  `momotaro-lex-bot`, role `TableAILexFulfillmentRole`), Terraform-managed
  (`infrastructure/`).

## Architecture — end-to-end flow

### Mode & table context (both flows)
The site loads a dine-in vs takeout chatbot config per page, so the frontend
knows the context. It passes:
- `orderMode = dine-in | takeout` as a **Lex session attribute**.
- For dine-in, `tableId` from the URL (e.g. `dine-in.momotarosushi.ca/table-2`
  → `tableId = table-2`) as a **session attribute**, mirroring the website.

`fulfill_order()` branches on `orderMode`.

### Dine-in (no payment)
```
chat order → Lex parses items (existing) → fulfill_order:
  • compute total server-side from MomotaroSushiMenu_DB (base price + option
    priceModifiers, 13% tax, round-half-up)
  • build order {orderId: DINE-…, orderType: dine-in, tableId, items, total, …}
  • SNS publish to order_events
      → DineInOrderWriter writes it to momotaroOrdersDatabase
      → KitchenDisplayPublisher → IoT printers/orders/print → prints + display
  • bot replies "Order placed — sent to the kitchen!"
```

### Takeout (Stripe Checkout link + polling confirmation)
```
chat order → Lex parses items → fulfill_order:
  • compute total server-side (same pricing as dine-in)
  • write order {orderId: TKOT-…, orderType: takeout, PENDING_PAYMENT, UNPAID}
    to momotaroOrdersDatabase
  • create a Stripe Checkout Session (mode=payment) with
    payment_intent_data.metadata.order_id = TKOT-…   ← puts order_id on the
    PaymentIntent so the EXISTING TakeoutIngress webhook works unchanged
  • bot replies with checkout.url ("Total $X.XX — pay here: …") and stashes
    pendingTakeoutOrderId in a session attribute

customer pays on Stripe's hosted page → payment_intent.succeeded webhook
  → TakeoutIngress → SNS order_events
      → TakeoutPaymentUpdater marks the order PAID
      → KitchenDisplayPublisher → prints

meanwhile (UI only): the chat page polls GET /order-status/{orderId} every ~3s
  → on PAID, injects a confirmation card into the chat widget:
    "✅ Payment received! Pick up at 2911 Dundas St West [Get Directions →]"
```

## Components

### 1. `fulfill_order()` rewrite (the container Lambda)
Replaces the current no-op summary. New logic:
1. Read `orderMode` (and `tableId` for dine-in) from session attributes; read the
   already-parsed items from the session (`parsedOrder`).
2. **Server-side pricing** — for each parsed item, resolve the canonical menu
   entry (the existing fuzzy match), sum `Price + matched option priceModifiers`,
   apply 13% tax, round-half-up to cents. Mirrors `checkout_api.py`'s
   `_compute_order_total_cents`. The total is never taken from the chat/LLM.
   Unknown/unmatched items → re-prompt the customer, do not order.
3. Generate `orderId` (`DINE-<rand>` or `TKOT-<rand>`), build the order in the
   **same shape the website produces** (fields consumed by the fanout:
   `orderId`, `orderType`, `items` [name, quantity, options, price], `total`,
   `notes`, `orderDate`, plus `tableId` for dine-in; `orderStatus`,
   `paymentStatus` for takeout).
4. **Dine-in branch:** `sns.publish` the order to the `order_events` topic. Reply
   "Order placed — sent to the kitchen!". (No direct DB write — `DineInOrderWriter`
   persists it, same as the website.)
5. **Takeout branch:** `put_item` the order as `PENDING_PAYMENT`/`UNPAID`; create a
   Stripe Checkout Session with `payment_intent_data.metadata.order_id`; reply with
   `checkout.url`; stash `pendingTakeoutOrderId`.
6. **Double-submit guard:** mark the session once an order is placed so a second
   `fulfill_order` in the same session can't create a duplicate ticket/charge.

### 2. Order-status endpoint (new, tiny)
`GET /order-status/{orderId}` → a small read-only Lambda doing one DynamoDB
`get_item`, returning **only** `{ orderId, paymentStatus, orderStatus }`. No
items, email, or card data. Order IDs are random, so nothing sensitive is
enumerable. This is the only new endpoint.

### 3. Frontend polling + confirmation injection (chat page)
- After the takeout payment link is returned, read `pendingTakeoutOrderId` from
  the Lex session attributes (lex-web-ui surfaces session state to the parent).
- Poll `GET /order-status/{orderId}` every ~3s. **Stop** on `PAID`, after a
  ~10-min timeout, or when a new order starts. Stateless — no persistent
  connection, nothing to leak or go stale.
- On `PAID`, append a bot-styled confirmation card to the chat transcript
  **directly via the lex-web-ui build** (which we own): receipt line (from the
  order the customer already saw), the pickup address `2911 Dundas St West`, and
  a **"Get Directions"** button linking to Google Maps. No Lex round-trip, so no
  session-timeout fragility.
- On timeout/failure: gentle fallback ("finish payment from the link, or check
  your email"). The order still processes via the webhook regardless.

### 4. IAM additions to `TableAILexFulfillmentRole`
- `sns:Publish` on the `order_events` topic (dine-in).
- `dynamodb:PutItem` on `momotaroOrdersDatabase` (takeout order creation).
- `ssm:GetParameter` on `/momotaro/prod/STRIPE_SECRET_KEY` + `kms:Decrypt`
  conditioned on `kms:ViaService = ssm.ca-central-1.amazonaws.com` (Stripe).

### 5. Lex session attributes contract
| Attribute | Set by | Used by |
|---|---|---|
| `orderMode` (`dine-in`/`takeout`) | frontend (per-page config) | `fulfill_order` branch |
| `tableId` (`table-2`) | frontend (URL param) | dine-in order build |
| `parsedOrder` (JSON items) | existing parse step | pricing + order build |
| `pendingTakeoutOrderId` (`TKOT-…`) | `fulfill_order` (takeout) | frontend polling |

## Error handling & edge cases
- **Unknown menu items:** bot asks for clarification; never orders/charges for an
  unpriceable item.
- **Price authority:** total always computed server-side from the menu.
- **Takeout abandoned:** order stays `PENDING_PAYMENT`, no webhook, nothing
  reaches the kitchen (correct); Stripe session expires on its own; polling times
  out with a gentle fallback.
- **Double-submit:** session guard blocks a duplicate order.
- **Stripe session creation fails:** bot apologizes, asks to retry; the unpaid
  order record lapses harmlessly.
- **Dine-in with no table:** bot asks "which table are you at?" rather than
  dropping a tableless ticket.
- **Status endpoint down / poll fails:** graceful degradation — order still
  processes via the webhook; customer relies on Stripe's email receipt.
- **Unchanged paths:** RAG Q&A, allergy, greeting, modification intents are not
  touched — only `fulfill_order` changes.

## What stays unchanged (reuse)
`TakeoutIngress`, `KitchenDisplayPublisher`, `DineInOrderWriter`,
`TakeoutPaymentUpdater`, `ReceiptEmailSender`, the SNS topic, the printer
(`listener.py`), and the kitchen display — all untouched. The chatbot simply
becomes another producer into the same pipeline, and takeout reuses the Stripe
+ webhook path verbatim.

## Out of scope / follow-ups
- The takeout webhook→print path has no print-level dedupe, so a Stripe webhook
  *retry* could print a takeout ticket twice — a pre-existing property of the
  website takeout flow too, worth a follow-up but not part of this feature.
- The fulfillment Lambda currently holds `OPENROUTER_API_KEY` / `GOOGLE_API_KEY`
  as plaintext env vars — should move to SSM/Secrets and be rotated (separate
  hardening task).
- Auto-appear "upgrade" to a WebSocket was considered and rejected in favor of
  polling (no stale connections, negligible cost at restaurant volume).
