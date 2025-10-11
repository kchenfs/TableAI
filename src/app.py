import json
import boto3
import os
import decimal

# Initialize Boto3 clients
dynamodb = boto3.resource('dynamodb')
menu_table = dynamodb.Table(os.environ['MENU_TABLE_NAME'])
orders_table = dynamodb.Table(os.environ['ORDERS_TABLE_NAME'])
bedrock_client = boto3.client('bedrock-runtime', region_name=os.environ['BEDROCK_REGION'])

class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        return super(DecimalEncoder, self).default(o)

def lambda_handler(event, context):
    """
    Main handler that routes the request based on the invocation source.
    """
    invocation_source = event.get('invocationSource')
    
    if invocation_source == 'DialogCodeHook':
        return handle_dialog(event)
    elif invocation_source == 'FulfillmentCodeHook':
        return fulfill_order(event)
    else:
        # Fallback for safety
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "Sorry, I'm not sure how to handle that."})

def handle_dialog(event):
    """
    Manages the conversation flow, validates input, and elicits slots.
    """
    intent = event['sessionState']['intent']
    slots = intent['slots']
    session_attrs = event['sessionState'].get('sessionAttributes', {})

    # 1. If OrderQuery is provided, parse it and ask about drinks
    if slots.get('OrderQuery') and not session_attrs.get('parsedOrder'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']
        
        try:
            # Fetch menu and call Bedrock to parse the food order
            menu_items = menu_table.scan().get('Items', [])
            menu_json_string = json.dumps(menu_items, cls=DecimalEncoder)
            
            prompt = f"""
You are a helpful restaurant order-taking assistant. A customer has made the following request: "{raw_order_text}".
Based on the menu provided below, parse the customer's request and extract the food items, quantities, and any specified modifiers. If a quantity is not specified for an item, assume the quantity is 1. Your response must be only a valid JSON object with a single key 'order_items' which is a list of objects. If an item is not on the menu, do not include it.

Menu: {menu_json_string}
"""
            parsed_order = invoke_bedrock(prompt)
            session_attrs['parsedOrder'] = json.dumps(parsed_order)
            
            # Elicit the DrinkQuery slot
            return elicit_slot(event, 'DrinkQuery', "Great, I've got that. Would you like anything to drink with your order?")

        except Exception as e:
            print(f"Error parsing food order with Bedrock: {e}")
            return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I had trouble understanding your order. Could you please try again?"})

    # 2. If DrinkQuery is provided, process it and ask for confirmation
    if slots.get('DrinkQuery') and not slots.get('Confirmation'):
        drink_text = slots['DrinkQuery']['value']['interpretedValue'].lower()
        parsed_order = json.loads(session_attrs.get('parsedOrder', '{}'))
        order_items = parsed_order.get('order_items', [])
        
        # Simple logic for drinks. Could also use Bedrock for more complex parsing.
        negatives = ['no', 'none', 'no thanks', 'not today', 'nothing']
        if drink_text not in negatives:
            order_items.append({'item_name': drink_text.strip(), 'quantity': 1, 'modifiers': []})
        
        parsed_order['order_items'] = order_items
        session_attrs['parsedOrder'] = json.dumps(parsed_order)
        
        # Create a summary message
        summary = "Okay, here is what I have for your order: "
        item_strings = [f"{item['quantity']} {item['item_name']}" for item in order_items]
        summary += ", ".join(item_strings)
        summary += ". Is that correct?"
        
        return elicit_slot(event, 'Confirmation', summary)

    # 3. If Confirmation is provided, delegate back to Lex or reset
    if slots.get('Confirmation'):
        confirmation_value = slots['Confirmation']['value']['interpretedValue']
        if confirmation_value.lower() == 'yes':
            # The dialog is complete and correct. Let Lex move to fulfillment.
            return delegate(event, session_attrs)
        else:
            # Order is incorrect. Reset and start over.
            return elicit_slot(event, 'OrderQuery', "My apologies. Let's start over. What would you like to order?", reset=True)

    # Default fallback: let Lex decide the next step
    return delegate(event, session_attrs)

def fulfill_order(event):
    """
    Called when all slots are filled and the intent is ready to be fulfilled.
    """
    try:
        session_attrs = event['sessionState'].get('sessionAttributes', {})
        final_order_str = session_attrs.get('parsedOrder', '{}')
        final_order = json.loads(final_order_str)
        
        # In a real application, you would save this final_order to the orders_table.
        # For example: orders_table.put_item(Item={'orderId': context.aws_request_id, 'order': final_order})
        print(f"Fulfilling final order: {final_order_str}")
        
        summary = "Thank you! Your order has been placed. Here is a summary: "
        item_strings = [f"{item['quantity']} {item['item_name']}" for item in final_order.get('order_items', [])]
        summary += ", ".join(item_strings)
        
        return close_dialog(event, 'Fulfilled', {'contentType': 'PlainText', 'content': summary})
        
    except Exception as e:
        print(f"Error fulfilling order: {e}")
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I'm sorry, I encountered an error while finalizing your order. Please try again."})

def invoke_bedrock(prompt):
    """Helper function to call Bedrock and parse the response."""
    claude_body = json.dumps({
        "prompt": f"\n\nHuman: {prompt}\n\nAssistant:",
        "max_tokens_to_sample": 1000,
        "temperature": 0.1,
    })
    response = bedrock_client.invoke_model(
        body=claude_body,
        modelId='anthropic.claude-instant-v1',
        accept='application/json',
        contentType='application/json'
    )
    response_body = json.loads(response.get('body').read())
    completion = response_body.get('completion', '{}').strip()
    return json.loads(completion)

# --- Lex V2 Response Helpers ---

def elicit_slot(event, slot_to_elicit, message_content, reset=False):
    intent = event['sessionState']['intent']
    session_attrs = event['sessionState'].get('sessionAttributes', {})
    
    if reset:
        # Clear all slots and session attributes to start over
        intent['slots'] = { "OrderQuery": None, "DrinkQuery": None, "Confirmation": None }
        session_attrs = {}

    return {
        'sessionState': {
            'dialogAction': {
                'type': 'ElicitSlot',
                'slotToElicit': slot_to_elicit,
            },
            'intent': intent,
            'sessionAttributes': session_attrs
        },
        'messages': [{'contentType': 'PlainText', 'content': message_content}]
    }

def delegate(event, session_attrs):
    return {
        'sessionState': {
            'dialogAction': {
                'type': 'Delegate'
            },
            'intent': event['sessionState']['intent'],
            'sessionAttributes': session_attrs
        }
    }

def close_dialog(event, fulfillment_state, message):
    event['sessionState']['intent']['state'] = fulfillment_state
    return {
        'sessionState': {
            'dialogAction': {
                'type': 'Close'
            },
            'intent': event['sessionState']['intent'],
            'sessionAttributes': event['sessionState'].get('sessionAttributes', {})
        },
        'messages': [message]
    }