# Print Two Copies + Takeout Order Confirmation — Design

## Problem

Two pieces of restaurant-feedback:

1. **Printing:** every order (dine-in or takeout) only prints one kitchen ticket. Staff want two — pure redundancy in case one gets lost, wet, or thrown out mid-shift.
2. **Order confirmation:** takeout customers sometimes call the restaurant to ask "did my order go through?" even after paying. The only confirmation today is an on-screen message and an existing (underused) email receipt that isn't reliably populated — the chatbot never collects an email at all, and the website's pre-payment contact fields are easy to skip without realizing why they matter.

## Goal

1. Every printed kitchen ticket comes out twice, automatically.
2. After a takeout customer pays — whether they ordered via the chatbot or the website cart — they land on a page that (a) immediately and unambiguously confirms the order with a full recap, and (b) offers to text and/or email a confirmation, with no pressure to provide either. Dine-in is explicitly out of scope: the customer is already at the table, and staff will confirm dine-in orders by walking over a physical receipt copy (which Feature 1 now makes trivial, since every order already prints twice).

## Feature 1: Print two copies

**Repo:** `momotaro-kitchen` (`printer-listener/listener.py`)

`print_order()` currently does one pass: header → item list → `printer.cut()`. Wrap that body in a loop that repeats it `PRINT_COPIES` times (env var, default `2`), each iteration ending in its own `cut()`, so two full physical tickets come off one MQTT message. The printer online/paper-level checks stay outside the loop and run once — if the printer dies between copy 1 and copy 2, the customer still gets one ticket (strictly better than today), and the existing `PRINT_ERROR` status reporting is unchanged.

No other component changes: SNS, `kitchen_display_publisher`, and the printer's own reliability logic (reconnect, paper detection) are untouched. This applies uniformly to both dine-in and takeout tickets.

## Feature 2: Takeout order confirmation

### Scope

**Takeout only.** Dine-in orders are unaffected — no opt-in form, no new confirmation page behavior for that path.

### User-facing flow

Both entry points — the website cart checkout and the chatbot — already redirect to a dedicated "thank you" page after Stripe confirms payment (`/completion` for the cart, `/order-complete` for the chatbot). Both pages get the same treatment:

1. **Immediate recap**, no interaction required: order number, itemized list, total, and a clear "Your order has been sent to the kitchen" statement. This alone answers "did it work?" regardless of whether the customer gives any contact info.
2. **Below the recap, an opt-in form**: phone number field, email field (both optional, either/both/neither), and a "Send confirmation" button. Submitting posts to a new backend endpoint that sends whichever channel(s) were filled in immediately.

**Checkout form changes (website cart only):** the existing pre-payment "Contact Info (Optional)" email/phone fields are removed from `CheckoutForm.tsx` — they added friction to the payment step without giving any real-time feedback about anything. In their place, add one line of reassurance text near the "Pay $X" button: *"You'll see your order confirmed immediately after payment — plus the option to get a text or email too."* This tells the customer what happens next before they commit, and the post-payment recap page is what actually delivers on it.

### Why a new endpoint instead of the existing email/SNS pipeline

The order is already paid, written to the database, and fanned out via SNS (which is what `receipt_email_sender` and `kitchen_display_publisher` already react to) *before* the customer ever sees the confirmation page. Contact info collected on that page necessarily arrives after that fan-out has already happened, so there's no way to hook it into the existing SNS-triggered workers — a new, directly-invoked endpoint sends the confirmation itself, on demand, at submit time. The existing `receipt_email_sender` worker is left as-is (it'll now rarely have a `receiptEmail` to act on at fan-out time, which is harmless — it already no-ops safely when the field is absent).

### New backend: order confirmation endpoint

**Repo:** `momotaro-fanout` (CDK app), as a new Lambda in `ingress/` (alongside `checkout_api`, `takeout_ingress`, `dine_in_ingress` — this is the natural home since it needs the same orders-table access and SES setup already configured there).

- **Request:** `POST` with JSON body `{"orderId": "TKOT-XXXXX", "phone": "4165551234", "email": "user@example.com"}` — `phone` and `email` are each optional; if both are absent, the endpoint no-ops and returns success (defensive; the frontend won't normally allow submitting an empty form).
- **Behavior:**
  1. Look up the order by `orderId` in the orders DynamoDB table. Not found → 404.
  2. If `phone` is present: normalize to E.164 (assume `+1` NANP prefix if not already present, since this is a Toronto restaurant), write it to the order's `phoneNumber` field, and publish an SMS via **AWS SNS** (`sns:Publish` with `PhoneNumber`, `MessageAttributes: {AWS.SNS.SMS.SMSType: Transactional}`) with a short confirmation: *"Momotaro Sushi: Order #{orderId} (${total}) is confirmed! We're preparing it now. Pickup: 2911 Dundas St West. Questions? 416-766-2888."* (comfortably under the 160-character single-segment limit).
  3. If `email` is present: write it to the order's `receiptEmail` field, and send the existing branded HTML receipt (the same template/composition logic `receipt_email_sender.py` already uses — factored into a small shared module in this repo so both the new endpoint and the existing worker call the same code, rather than duplicating the email-building logic).
  4. Respond `{"success": true, "smsSent": bool, "emailSent": bool}` (or a friendly error) so the page can show "Confirmation sent!" or a gentle failure message. A failure here never implies the order itself failed — payment and kitchen routing already succeeded independently.
- **IAM:** a dedicated role with `dynamodb:GetItem` + `dynamodb:UpdateItem` on the orders table, `sns:Publish`, and `ses:SendEmail`/`ses:SendRawEmail` — mirroring the existing per-Lambda role pattern in this stack.
- **Security posture:** no auth on this endpoint, consistent with the existing order-status endpoint's posture — order IDs are random 5-character suffixes, not enumerable. Unlike the read-only status endpoint, this one writes; the accepted risk is that a guessed ID lets someone attach a phone/email to someone else's order and trigger a text/email to themselves, not to the real customer — no payment, order contents, or existing PII are exposed or alterable beyond that. This is judged acceptable for a single small restaurant's volume; revisit if abuse is ever observed.

### Order recap data

- **Website cart:** the browser already holds the full cart, items, and total in memory right before payment (it's what built the payment amount). Rather than adding a new fetch, that data is cached to `sessionStorage` immediately before calling `stripe.confirmPayment`, and read back on `/completion` after the redirect. The order's `orderId` (already generated client-side before payment) is appended to the Stripe `return_url` so the page can label everything correctly and know what to POST against.
- **Chatbot:** `/order-complete` is a fresh page load with no prior browser state, so it fetches the recap from the backend. The existing order-status Lambda (`TableAI` repo, `status_lambda/status_app.py`) currently returns only `{orderId, paymentStatus, orderStatus}` by deliberate design; it's extended to also return `items` and `total` (still excluding `receiptEmail`, `phoneNumber`, and any payment/card details). This requires the chatbot's Stripe Checkout `success_url` to carry the order id (`.../order-complete?orderId={order_id}`) — a change to `fulfill_order`'s takeout branch in `lambda_container_project/app.py` (`TableAI` repo) — so the page knows which order to fetch.
- Both pages render the recap + opt-in form via a **shared React component** in `momotaro-next` (e.g. `OrderConfirmation` or similar), since the UI is now identical on both pages — avoids duplicating markup and the POST-to-confirmation-endpoint logic.

### SMS provider: AWS SNS

Chosen over Twilio (adds a new vendor account, a monthly per-number fee, and a new secret to manage, for capabilities — two-way messaging, delivery-receipt webhooks — this feature doesn't need) and over Telegram (structurally can't push a message to an arbitrary phone number; a bot can only message someone who has already started a chat with it, which doesn't fit "text an unknown customer"). AWS SNS needs no new vendor relationship, costs a fraction of a cent per message to Canadian numbers, and reuses IAM/boto3 patterns already established in this codebase. **Setup note:** new AWS accounts often start with a very low default monthly SMS spending cap; a Service Quotas increase request (self-service, typically fast) will likely be needed before real messages can be sent at any volume.

## Components touched (by repo)

| Repo | Change |
|---|---|
| `momotaro-kitchen` | `printer-listener/listener.py`: print `PRINT_COPIES` (default 2) copies per order |
| `momotaro-fanout` | New `ingress/` Lambda: order confirmation endpoint (DynamoDB update + SNS SMS + SES email), new IAM role, shared email-composition module |
| `TableAI` | Extend `status_lambda/status_app.py` response with `items`/`total`; add `?orderId=` to the chatbot's Stripe success URL in `lambda_container_project/app.py` |
| `momotaro-next` | `CheckoutForm.tsx`: remove pre-payment contact fields, add reassurance copy, cache recap to `sessionStorage`, pass `orderId` via `return_url`; `/completion` and `/order-complete` pages: shared recap + opt-in component |

## Error handling & edge cases

- Confirmation endpoint failure (SMS or email) never affects the order itself — it's a best-effort courtesy layered on top of an already-successful, already-kitchen-routed order.
- Empty submission (neither phone nor email) is a harmless no-op, not an error.
- If the recap fetch fails on `/order-complete` (network hiccup, bad/missing `orderId`), the page still shows the "payment succeeded" confirmation without the itemized recap, rather than blocking on it.
- Printing: if the printer fails mid-way through the second copy, the first copy still printed — logged and reported the same way a single-copy failure is today.

## Testing

- **Printer:** unit-test-equivalent manual verification (this is a physical-device script) — confirm two physical tickets print per test order, and that the existing offline/no-paper/paper-low status reporting still fires exactly once per order (not duplicated) when using the loop.
- **Confirmation endpoint:** unit tests for phone normalization, the no-op-on-empty-input case, DynamoDB not-found handling, and that SMS/email sends are attempted independently (a failure in one doesn't block the other).
- **Frontend:** manual verification of both `/completion` (website cart) and `/order-complete` (chatbot) — recap renders correctly from `sessionStorage` and from the extended status endpoint respectively; opt-in form submits and shows success/failure feedback; reassurance copy renders near the Pay button with the old contact fields gone.

## Out of scope / follow-ups

- Dine-in confirmation notifications (explicitly deferred; physical receipt handoff via Feature 1's second copy covers it for now).
- Any richer SMS features (delivery-status texts, two-way replies, "your order is ready for pickup" follow-up text) — this design covers only the initial order-confirmed message.
- Consolidating `/completion` and `/order-complete` into a single page/route — they remain separate pages that share a component, not a single unified route.
