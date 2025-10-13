# /src/app.py

import json
import boto3
import os
import decimal
from openai import OpenAI  # <-- ADDED import

# Initialize Boto3 clients for DynamoDB
dynamodb = boto3.resource('dynamodb')
menu_table = dynamodb.Table(os.environ['MENU_TABLE_NAME'])
orders_table = dynamodb.Table(os.environ['ORDERS_TABLE_NAME'])

# --- REPLACED BEDROCK WITH OPENROUTER CLIENT ---
# This uses the OPENROUTER_API_KEY environment variable you'll set in Lambda
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
)

# Global cache for the menu to reduce DynamoDB reads
menu_cache = None
# In-memory representation of the menu for easy lookups
menu_data_cache = None


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            # Convert Decimal to float or int
            if o % 1 == 0:
                return int(o)
            else:
                return float(o)
        return super(DecimalEncoder, self).default(o)


def get_menu(force_refresh=False):
    """
    Fetches the menu from DynamoDB and caches it.
    Returns both the JSON string for Bedrock and a Python dict for logic.
    """
    global menu_cache, menu_data_cache
    if menu_cache is None or force_refresh:
        print("Cache miss or force refresh. Fetching menu from DynamoDB.")
        response = menu_table.scan()
        menu_data_cache = response.get('Items', [])
        menu_cache = json.dumps(menu_data_cache, cls=DecimalEncoder)
    else:
        print("Cache hit. Using cached menu.")
    return menu_cache, menu_data_cache


def find_item_in_menu(item_name):
    """Helper to find an item's full data from the cached menu."""
    _, menu_data = get_menu()
    for item in menu_data:
        # Case-insensitive search for robustness
        if item.get('ItemName', '').lower() == item_name.lower():
            return item
    return None


def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")
    invocation_source = event.get('invocationSource')
    
    if invocation_source == 'DialogCodeHook':
        return handle_dialog(event)
    elif invocation_source == 'FulfillmentCodeHook':
        return fulfill_order(event)
    else:
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "Sorry, I'm not sure how to handle that."})


def handle_dialog(event):
    intent = event['sessionState']['intent']
    slots = intent['slots']
    session_attrs = event['sessionState'].get('sessionAttributes', {})
    
    # --- Main Logic Flow ---

    # 1. Handle the response to the final confirmation prompt
    confirmation_state = intent.get('confirmationState')
    if confirmation_state == 'Confirmed':
        return delegate(event, session_attrs)
    if confirmation_state == 'Denied':
        return elicit_slot(event, 'OrderQuery', "My apologies. Let's start over. What would you like to order?", reset=True)

    # 2. Handle a user providing a choice for a complex item
    if session_attrs.get('currentItemToConfigure'):
        session_attrs.pop('currentItemToConfigure', None)
        return elicit_slot(event, 'DrinkQuery', "Got it. Anything to drink with that?")

    # 3. Elicit the first slot if the conversation has just started
    if not slots.get('OrderQuery'):
        return elicit_slot(event, 'OrderQuery', "Certainly, what would you like to order?")

    # 4. If OrderQuery is filled, call the LLM to parse everything.
    if slots.get('OrderQuery') and not session_attrs.get('initialParseComplete'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']
        
        try:
            menu_json_string, _ = get_menu()
            prompt = f"""
You are an intelligent restaurant assistant. A customer said: "{raw_order_text}".
Based on the menu provided below, extract all items the customer ordered. Your response must be a valid JSON object with a single key 'order_items', which is a list of objects. Each object in the list must include the item's "item_name", "quantity", and its "Category" exactly as it appears in the menu. If a quantity is not specified, assume 1. If an item is not on the menu, do not include it.

Menu: {menu_json_string}
"""
            parsed_result = invoke_openrouter(prompt) # <-- UPDATED
            order_items = parsed_result.get('order_items', [])
            
            session_attrs['parsedOrder'] = json.dumps({'order_items': order_items})
            session_attrs['initialParseComplete'] = "true"
            
            if not order_items:
                return elicit_slot(event, 'OrderQuery', "I'm sorry, none of those items seem to be on our menu. What would you like to order?", reset=True)
            
            first_item_name = order_items[0].get('item_name')
            menu_item_data = find_item_in_menu(first_item_name)
            
            if menu_item_data and 'Options' in menu_item_data:
                session_attrs['currentItemToConfigure'] = json.dumps(menu_item_data)
                required_option = next((opt for opt in menu_item_data['Options'] if opt.get('required')), None)
                if required_option:
                    option_name = required_option.get('name')
                    choices = ", ".join([item.get('name') for item in required_option.get('items', [])])
                    prompt_text = f"For the {first_item_name}, what {option_name} would you like? Your choices are: {choices}."
                    return elicit_slot(event, 'OptionChoice', prompt_text)
            
            has_food = any(item.get('Category') not in ['Drink', 'Alcohol'] for item in order_items)
            has_drinks = any(item.get('Category') in ['Drink', 'Alcohol'] for item in order_items)

            if has_food and has_drinks:
                summary = "Okay, I have: " + ", ".join([f"{item['quantity']} {item['item_name']}" for item in order_items]) + ". Is that correct?"
                return confirm_intent(event, summary)
            elif has_food and not has_drinks:
                return elicit_slot(event, 'DrinkQuery', "I've got your food order. Would you like anything to drink?")
            elif has_drinks and not has_food:
                drink_names = ", ".join([f"{item['quantity']} {item['item_name']}" for item in order_items])
                return elicit_slot(event, 'OrderQuery', f"Okay, I have {drink_names}. What would you like to eat with that?")
            else:
                return elicit_slot(event, 'OrderQuery', "I couldn't quite understand that. What would you like to order?", reset=True)

        except Exception as e:
            print(f"Error parsing initial order with OpenRouter: {e}")
            return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I had trouble understanding your order. Could you please try again?"})

    # 5. If DrinkQuery is filled (because it was asked for)
    if slots.get('DrinkQuery'):
        parsed_order = json.loads(session_attrs.get('parsedOrder', '{}'))
        order_items = parsed_order.get('order_items', [])
        drink_text = slots['DrinkQuery']['value']['interpretedValue']
        
        try:
            menu_json_string, _ = get_menu()
            prompt = f"A customer ordered a drink: \"{drink_text}\". Based on the menu, find the exact drink item. Respond in JSON with one key 'found_drink_items' (a list). If not on menu, return an empty list. Menu: {menu_json_string}"
            validated_drink_result = invoke_openrouter(prompt) # <-- UPDATED
            found_drinks = validated_drink_result.get('found_drink_items', [])

            if not found_drinks:
                return elicit_slot(event, 'DrinkQuery', "Sorry, that drink isn't on the menu. What would you like?")
            
            order_items.extend(found_drinks)
        except Exception as e:
            print(f"Error validating drink: {e}")
            order_items.append({'item_name': drink_text.strip(), 'quantity': 1, 'Category': 'Drink'})

        session_attrs['parsedOrder'] = json.dumps({'order_items': order_items})
        summary = "Okay, I have: " + ", ".join([f"{item['quantity']} {item['item_name']}" for item in order_items]) + ". Is that correct?"
        return confirm_intent(event, summary)

    return delegate(event, session_attrs)


def fulfill_order(event):
    try:
        session_attrs = event['sessionState'].get('sessionAttributes', {})
        final_order_str = session_attrs.get('parsedOrder', '{}')
        final_order = json.loads(final_order_str)
        print(f"Fulfilling final order: {final_order_str}")
        summary = "Thank you! Your order for " + ", ".join([f"{item['quantity']} {item['item_name']}" for item in final_order.get('order_items', [])]) + " has been placed."
        # orders_table.put_item(Item=final_order)
        return close_dialog(event, 'Fulfilled', {'contentType': 'PlainText', 'content': summary})
    except Exception as e:
        print(f"Error fulfilling order: {e}")
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I'm sorry, I encountered an error while finalizing your order."})

# --- NEW FUNCTION TO CALL OPENROUTER ---
def invoke_openrouter(prompt):
    """
    Invokes the OpenRouter API with a given prompt and returns the parsed JSON response.
    """
    print("Invoking OpenRouter with Llama 4 Maverick...")
    try:
        completion = client.chat.completions.create(
          # Optional headers to identify your app on OpenRouter rankings
          extra_headers={
            "HTTP-Referer": "YOUR_SITE_URL",  # Replace with your actual site
            "X-Title": "YOUR_APP_NAME",      # Replace with your app name
          },
          model="meta-llama/llama-4-maverick:free",
          messages=[
            {
              "role": "user",
              "content": prompt,
            }
          ],
          # Tell the model to return a valid JSON object
          response_format={"type": "json_object"},
        )
        
        response_text = completion.choices[0].message.content
        print(f"Received from OpenRouter: {response_text}")
        return json.loads(response_text)
    except Exception as e:
        print(f"Error calling OpenRouter API: {e}")
        # Return an empty dict or raise the exception, depending on desired error handling
        return {}

# --- Lex V2 Response Helpers ---

def elicit_slot(event, slot_to_elicit, message_content, reset=False):
    intent = event['sessionState']['intent']
    session_attrs = event['sessionState'].get('sessionAttributes', {})
    if reset:
        intent['slots'] = { "OrderQuery": None, "DrinkQuery": None, "OptionChoice": None }
        session_attrs = {}
    return {
        'sessionState': {
            'dialogAction': {'type': 'ElicitSlot', 'slotToElicit': slot_to_elicit},
            'intent': intent, 'sessionAttributes': session_attrs
        },
        'messages': [{'contentType': 'PlainText', 'content': message_content}]
    }

def confirm_intent(event, message_content):
    intent = event['sessionState']['intent']
    session_attrs = event['sessionState'].get('sessionAttributes', {})
    return {
        'sessionState': {
            'dialogAction': {'type': 'ConfirmIntent'},
            'intent': intent, 'sessionAttributes': session_attrs
        },
        'messages': [{'contentType': 'PlainText', 'content': message_content}]
    }

def delegate(event, session_attrs):
    return {
        'sessionState': {
            'dialogAction': {'type': 'Delegate'},
            'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs
        }
    }

def close_dialog(event, fulfillment_state, message):
    event['sessionState']['intent']['state'] = fulfillment_state
    return {
        'sessionState': {
            'dialogAction': {'type': 'Close'},
            'intent': event['sessionState']['intent'],
            'sessionAttributes': event['sessionState'].get('sessionAttributes', {})
        },
        'messages': [message]
    }