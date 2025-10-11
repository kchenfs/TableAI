import json
import boto3
import os
import decimal

# Initialize Boto3 clients using environment variables from Terraform
dynamodb = boto3.resource('dynamodb')
menu_table = dynamodb.Table(os.environ['MENU_TABLE_NAME'])
orders_table = dynamodb.Table(os.environ['ORDERS_TABLE_NAME'])

# Bedrock client is initialized with the region from the environment variables
bedrock_client = boto3.client('bedrock-runtime', region_name=os.environ['BEDROCK_REGION'])

class DecimalEncoder(json.JSONEncoder):
    """Helper class to convert a DynamoDB item's Decimal types to floats."""
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        return super(DecimalEncoder, self).default(o)

def lambda_handler(event, context):
    """
    Main handler that Lex invokes.
    Routes the request based on the intent name.
    """
    intent_name = event['sessionState']['intent']['name']

    if intent_name == 'OrderFood':
        return handle_order_food(event)
    else:
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "Sorry, I can only take food orders."})

def handle_order_food(event):
    """
    Handles the logic for the OrderFood intent.
    - Extracts the raw order query.
    - Fetches the menu from DynamoDB.
    - Constructs a prompt and calls Bedrock to parse the order.
    - Returns a confirmation to the user.
    """
    slots = event['sessionState']['intent']['slots']
    raw_order_text = slots.get('OrderQuery', {}).get('value', {}).get('interpretedValue')

    if not raw_order_text:
        return elicit_slot(event, 'OrderQuery')

    try:
        # 1. Fetch the entire menu from DynamoDB
        response = menu_table.scan()
        menu_items = response.get('Items', [])
        menu_json_string = json.dumps(menu_items, cls=DecimalEncoder)

        # 2. Construct the prompt for Bedrock
        prompt = f"""
You are a helpful restaurant order-taking assistant. A customer has made the following request: "{raw_order_text}".

Based on the menu provided below, parse the customer's request and extract the items, quantities, and any specified modifiers. Your response must be only a valid JSON object with a single key 'order_items' which is a list of objects, each with 'item_name', 'quantity', and an optional 'modifiers' list. If an item is not on the menu, do not include it.

Menu:
{menu_json_string}
"""

        # 3. Call the Bedrock model (e.g., Claude)
        claude_body = json.dumps({
            "prompt": f"\n\nHuman: {prompt}\n\nAssistant:",
            "max_tokens_to_sample": 1000,
            "temperature": 0.1,
        })

        response = bedrock_client.invoke_model(
            body=claude_body,
            modelId='anthropic.claude-instant-v1', # You can swap this for other models like Claude 3 Sonnet
            accept='application/json',
            contentType='application/json'
        )

        response_body = json.loads(response.get('body').read())
        parsed_order_text = response_body.get('completion').strip()

        # 4. Process the response from Bedrock
        # In a real application, you would save this parsed_order_text to the orders_table.
        print(f"Bedrock parsed order: {parsed_order_text}")

        # 5. Create a user-friendly confirmation message
        confirmation_message = f"Thank you! I've processed your order. Here is what I understood: {parsed_order_text}"

        return close_dialog(event, 'Fulfilled', {'contentType': 'PlainText', 'content': confirmation_message})

    except Exception as e:
        print(f"Error processing order: {e}")
        error_message = "I'm sorry, I encountered an error while processing your order. Please try again."
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': error_message})

def elicit_slot(event, slot_to_elicit):
    """Tells Lex to ask the user for a specific slot value."""
    return {
        'sessionState': {
            'dialogAction': {
                'type': 'ElicitSlot',
                'slotToElicit': slot_to_elicit,
            },
            'intent': event['sessionState']['intent']
        }
    }

def close_dialog(event, fulfillment_state, message):
    """Formats the final response to Lex."""
    response = {
        'sessionState': {
            'dialogAction': {
                'type': 'Close'
            },
            'intent': event['sessionState']['intent']
        },
        'messages': [message]
    }
    response['sessionState']['intent']['state'] = fulfillment_state
    return response