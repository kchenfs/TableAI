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
