import json
import os
import urllib.parse
import urllib.request
import boto3

# Fallback alert: fires on every PAID takeout order fanned out from the SNS
# order_events topic (independent of the printer), and DMs the order details to
# a Telegram chat so staff still get the order if the kitchen ticket doesn't print.

_ssm = boto3.client('ssm')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TOKEN_PARAM = os.environ.get('TELEGRAM_TOKEN_PARAM', '/momotaro/prod/TELEGRAM_BOT_TOKEN')
_TOKEN = None


def _token():
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = _ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)['Parameter']['Value']
    return _TOKEN


def _format_order(order):
    oid = order.get('orderId', 'UNKNOWN')
    when = (order.get('orderDate') or '')[:19].replace('T', ' ')
    lines = ["\U0001F9FE NEW PAID TAKEOUT ORDER", f"Order: {oid}"]
    if when:
        lines.append(f"Time: {when}")
    lines.append("")
    lines.append("Items:")
    for it in order.get('items', []) or []:
        qty = it.get('quantity', 1)
        name = it.get('name', 'Item')
        opts = (it.get('options') or '').strip()
        lines.append(f"  - {qty}x {name}" + (f" ({opts})" if opts else ""))
    try:
        total = float(order.get('total', 0) or 0)
    except (TypeError, ValueError):
        total = 0.0
    lines.append("")
    lines.append(f"TOTAL: ${total:.2f}")
    pd = order.get('paymentDetails') or {}
    if pd.get('last4'):
        lines.append(f"Paid: {pd.get('brand', 'card')} ****{pd.get('last4')}")
    if order.get('receiptEmail'):
        lines.append(f"Customer: {order.get('receiptEmail')}")
    notes = (order.get('notes') or '').strip()
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


def _send(text):
    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def handler(event, context):
    for record in event.get('Records', []):
        try:
            order = json.loads(record['Sns']['Message'])
            status = _send(_format_order(order))
            print(f"Telegram alert sent for {order.get('orderId')}: HTTP {status}")
        except Exception as e:
            # A notification failure must never retry/block the order fan-out.
            print(f"Telegram notify failed: {e}")
    return {'statusCode': 200}
