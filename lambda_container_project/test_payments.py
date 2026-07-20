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
