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
