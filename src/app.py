# app.py
import json
import boto3
import os
import decimal
import time
import re
from openai import OpenAI
import traceback
import random

# --- MODIFIED IMPORTS for Google Gemini ---
import google.generativeai as genai
import numpy as np
# ----------------------------------------

# Environment variables
MENU_TABLE_NAME = os.environ['MENU_TABLE_NAME']
ORDERS_TABLE_NAME = os.environ['ORDERS_TABLE_NAME']
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL_NAME = os.environ.get("MODEL_NAME", "meta-llama/llama-3.3-70b-instruct:free")
# --- NEW: Google AI Environment Variable ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
# -------------------------------------------

# AWS and AI model initialization
dynamodb = boto3.resource('dynamodb')
menu_table = dynamodb.Table(MENU_TABLE_NAME)
orders_table = dynamodb.Table(ORDERS_TABLE_NAME)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# --- MODIFIED: Configure Google AI Client ---
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    GEMINI_EMBEDDING_MODEL = 'models/embedding-001'
else:
    print("Warning: GOOGLE_API_KEY environment variable not set.")
# -------------------------------------------

# Global caches
_menu_cache_timestamp = 0
_menu_cache_ttl_seconds = int(os.environ.get("MENU_CACHE_TTL", 300))
_menu_raw = None
_menu_lookup = None
_menu_embeddings_cache = None

class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        return super(DecimalEncoder, self).default(o)

def _normalize_name(s):
    if not isinstance(s, str): return ""
    return re.sub(r'\s+', ' ', s.strip().lower())

def _unwrap_dynamodb_value(value):
    if isinstance(value, dict):
        if 'S' in value: return value['S']
        elif 'N' in value: return float(value['N'])
        elif 'BOOL' in value: return value['BOOL']
        elif 'L' in value: return [_unwrap_dynamodb_value(item) for item in value['L']]
        elif 'M' in value: return {k: _unwrap_dynamodb_value(v) for k, v in value['M'].items()}
        else: return {k: _unwrap_dynamodb_value(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_unwrap_dynamodb_value(item) for item in value]
    return value

def _build_menu_lookup(items):
    lookup = {}
    for item in items:
        unwrapped_item = _unwrap_dynamodb_value(item)
        raw_name = unwrapped_item.get('ItemName', '')
        if not raw_name: continue
        
        normalized = _normalize_name(raw_name)
        options_struct = {}
        raw_options_list = unwrapped_item.get('Options', [])
        
        if not isinstance(raw_options_list, list): raw_options_list = []
        
        for opt in raw_options_list:
            if not isinstance(opt, dict): continue
            opt_name_raw = opt.get('name', '')
            if not opt_name_raw: continue
            
            opt_name = _normalize_name(opt_name_raw)
            choices = []
            items_list = opt.get('items', [])
            if not isinstance(items_list, list): items_list = []
            
            for choice_item in items_list:
                if not isinstance(choice_item, dict): continue
                choice_name_raw = choice_item.get('name', '')
                if choice_name_raw: choices.append(_normalize_name(choice_name_raw))
            
            required = opt.get('required', False)
            options_struct[opt_name] = {"raw_name": opt_name_raw, "choices": choices, "required": bool(required)}
        
        lookup[normalized] = {
            "raw_item": unwrapped_item, "normalized_name": normalized, "options": options_struct,
            "category": unwrapped_item.get('Category'), "price": unwrapped_item.get('Price'),
            "item_number": unwrapped_item.get('ItemNumber')
        }
    return lookup

def get_menu(force_refresh=False):
    global _menu_cache_timestamp, _menu_raw, _menu_lookup, _menu_embeddings_cache
    now = int(time.time())
    if force_refresh or _menu_raw is None or (now - _menu_cache_timestamp) > _menu_cache_ttl_seconds:
        print("Refreshing menu cache...")
        try:
            items = menu_table.scan().get('Items', [])
            _menu_raw, _menu_lookup, _menu_cache_timestamp = items, _build_menu_lookup(items), now
            embeddings = []
            for item in items:
                unwrapped_item = _unwrap_dynamodb_value(item)
                embedding_value = unwrapped_item.get('ItemEmbedding')
                if embedding_value and isinstance(embedding_value, list):
                    embeddings.append({"normalized_key": _normalize_name(unwrapped_item.get('ItemName', '')), "embedding": np.array([float(x) for x in embedding_value])})
            _menu_embeddings_cache = embeddings
            print(f"Loaded {len(_menu_embeddings_cache)} embeddings.")
        except Exception as e:
            print(f"ERROR loading menu: {e}"); traceback.print_exc(); raise
    return _menu_raw, _menu_lookup, _menu_embeddings_cache

def _fuzzy_find(normalized_name, menu_lookup, embeddings_cache, cutoff=0.6):
    if not normalized_name: return None, 0.0
    if normalized_name in menu_lookup: return normalized_name, 1.0
    try:
        query_embedding = genai.embed_content(model=GEMINI_EMBEDDING_MODEL, content=normalized_name, task_type="RETRIEVAL_QUERY")['embedding']
    except Exception as e:
        print(f"Error getting embedding for '{normalized_name}': {e}"); return None, 0.0
    best_score, best_match_key = -1, None
    for item_embedding in embeddings_cache:
        v1, v2 = query_embedding, item_embedding['embedding']
        similarity = np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))
        if similarity > best_score: best_score, best_match_key = similarity, item_embedding['normalized_key']
    return (best_match_key, best_score) if best_score >= cutoff else (None, 0.0)

def _check_if_option_in_item_name(parsed_name, menu_entry):
    detected_options, customer_words = {}, _normalize_name(parsed_name).split()
    for _, opt_meta in menu_entry['options'].items():
        for choice_normalized in opt_meta.get('choices', []):
            if choice_normalized in customer_words:
                detected_options[opt_meta['raw_name']] = choice_normalized; break
    return detected_options

def lambda_handler(event, context):
    print("--- NEW INVOCATION ---")
    print(f"EVENT from Lex: {json.dumps(event)}")
    
    intent_name = event['sessionState']['intent']['name']
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}

    # --- ROUTER LOGIC ---

    # 1. Handle FallbackIntent by transforming it into an OrderFood intent
    if intent_name == 'FallbackIntent':
        print("HANDLER: Caught FallbackIntent. Assuming it's an order.")
        transcript = event.get('inputTranscript', '')
        session_attrs['is_fallback_order'] = 'true' # Flag for mitigation
        event['sessionState']['sessionAttributes'] = session_attrs
        event['sessionState']['intent']['name'] = 'OrderFood'
        if 'slots' not in event['sessionState']['intent']:
            event['sessionState']['intent']['slots'] = {}
        event['sessionState']['intent']['slots']['OrderQuery'] = {'value': {'originalValue': transcript, 'interpretedValue': transcript, 'resolvedValues': []}, 'shape': 'Scalar'}
        print(f"HANDLER: Transformed Fallback to OrderFood with OrderQuery: '{transcript}'")
        return handle_dialog(event)

    # 2. Handle GreetingIntent by welcoming and prompting for an order
    if intent_name == 'GreetingIntent':
        greetings = ["Hello! I'm ready to take your order. What can I get for you?", "Hi there! What would you like to order today?", "Welcome! Tell me what you'd like to eat."]
        response_message = random.choice(greetings)
        
        response = {
            'sessionState': {
                'dialogAction': {
                    'type': 'ElicitSlot',
                    'slotToElicit': 'OrderQuery'
                },
                'intent': {
                    'name': 'OrderFood',
                    'slots': {
                        'OrderQuery': None,
                        'DrinkQuery': None,
                        'OptionChoice': None
                    },
                    'state': 'InProgress'
                },
                'sessionAttributes': {}
            },
            'messages': [{'contentType': 'PlainText', 'content': response_message}]
        }
        print(f"RESPONSE to Lex: {json.dumps(response)}") # <-- ADDED
        return response

    # 3. Handle regular dialog and fulfillment hooks
    invocation_source = event.get('invocationSource')
    if invocation_source == 'DialogCodeHook':
        return handle_dialog(event)
    elif invocation_source == 'FulfillmentCodeHook':
        return fulfill_order(event)
    
    return close_dialog(event, session_attrs, 'Failed', {'contentType': 'PlainText', 'content': "Sorry, I couldn't handle your request."})


def handle_dialog(event):
    intent = event['sessionState']['intent']
    slots = intent.get('slots', {})
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}
    confirmation_state = intent.get('confirmationState')

    if confirmation_state == 'Confirmed':
        return delegate(event, session_attrs)
    if confirmation_state == 'Denied':
        return elicit_slot(event, session_attrs, 'OrderQuery', "Okay — let's start over. What would you like to order?", reset=True)

    if session_attrs.get('currentItemToConfigure') and slots.get('OptionChoice') and slots.get('OptionChoice').get('value'):
        current_item = json.loads(session_attrs.pop('currentItemToConfigure'))
        option_name_to_set = session_attrs.pop('optionToConfigure')
        parsed_order = json.loads(session_attrs['parsedOrder'])
        order_items = parsed_order.get('order_items', [])
        choice_value = slots['OptionChoice']['value']['interpretedValue']
        for i, item in enumerate(order_items):
            if item.get('normalized_key') == current_item.get('normalized_key'):
                if 'options' not in item or item['options'] is None: item['options'] = {}
                item['options'][option_name_to_set] = choice_value; order_items[i] = item; break
        session_attrs['parsedOrder'] = json.dumps({"order_items": order_items}, cls=DecimalEncoder)
        slots['OptionChoice'] = None

    if not slots.get('OrderQuery') and not session_attrs.get('parsedOrder'):
        return elicit_slot(event, session_attrs, 'OrderQuery', "Sure — what would you like to order?")

    if slots.get('OrderQuery') and not session_attrs.get('initialParseComplete'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']
        try:
            # This check is no longer needed here because it's handled in the parser function
            parsed_result = invoke_openrouter_parser(raw_order_text)
            
            normalized_items = []
            _, menu_lookup, embeddings_cache = get_menu()
            for it in parsed_result.get('order_items', []):
                if not isinstance(it, dict): continue
                parsed_name = it.get('item_name', '')
                if not parsed_name: continue
                quantity = int(it.get('quantity', 1))
                options = it.get('options') if isinstance(it.get('options'), dict) else {}
                best_key, _ = _fuzzy_find(_normalize_name(parsed_name), menu_lookup, embeddings_cache)
                if best_key:
                    menu_entry = menu_lookup[best_key]
                    detected_options = _check_if_option_in_item_name(parsed_name, menu_entry)
                    validated_options = {**detected_options, **options}
                    normalized_items.append({"item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": best_key, "quantity": quantity, "options": validated_options, "category": menu_entry.get('category'), "price": menu_entry.get('price'), "item_number": menu_entry.get('item_number')})
                else:
                    normalized_items.append({"item_name": parsed_name, "normalized_key": None, "quantity": quantity, "options": options})
            
            # This mitigation logic will now be reached correctly
            if session_attrs.pop('is_fallback_order', None) and not any(item.get('normalized_key') for item in normalized_items):
                print("MITIGATION: Fallback triggered but no valid menu items found.")
                message = "I'm sorry, I can only take food and drink orders. I didn't recognize any menu items in your request. Could you try again?"
                return elicit_slot(event, {}, 'OrderQuery', message, reset=True)
                
            session_attrs['parsedOrder'] = json.dumps({"order_items": normalized_items}, cls=DecimalEncoder)
            session_attrs['initialParseComplete'] = "true"
        except Exception as e:
            print(f"Error during parsing: {e}"); traceback.print_exc()
            return close_dialog(event, session_attrs, 'Failed', {'contentType': 'PlainText', 'content': "I had trouble understanding that. Could you please try again?"})
    
    # ... The rest of the handle_dialog function remains the same ...
    if session_attrs.get('parsedOrder'):
        current_order = json.loads(session_attrs['parsedOrder'])
        normalized_items = current_order.get('order_items', [])
        _, menu_lookup, _ = get_menu()
        unmatched = [i for i in normalized_items if not i.get('normalized_key')]
        if unmatched:
            return elicit_slot(event, session_attrs, 'OrderQuery', f"I couldn't find '{unmatched[0]['item_name']}' on the menu. Could you clarify that part of your order?")
        for ni in normalized_items:
            if ni.get('normalized_key'):
                entry = menu_lookup[ni['normalized_key']]
                for opt_key_norm, opt_meta in entry['options'].items():
                    if opt_meta.get('required'):
                        provided_options = ni.get('options', {}) or {}
                        is_provided = any(_normalize_name(k) == opt_key_norm or k == opt_meta.get('raw_name') for k in provided_options.keys())
                        if not is_provided:
                            session_attrs['currentItemToConfigure'] = json.dumps(ni, cls=DecimalEncoder)
                            option_name = opt_meta.get('raw_name')
                            session_attrs['optionToConfigure'] = option_name
                            choices_text = ", ".join(opt_meta.get('choices', []))
                            message = f"For your {ni['item_name']}, which {option_name} would you like? Choices are: {choices_text}."
                            return elicit_slot(event, session_attrs, 'OptionChoice', message)
        has_food = any(i.get('category') and 'drink' not in str(i.get('category','')).lower() for i in normalized_items)
        has_drink = any(i.get('category') and 'drink' in str(i.get('category','')).lower() for i in normalized_items)
        if has_food and not has_drink and not slots.get('DrinkQuery'):
            return elicit_slot(event, session_attrs, 'DrinkQuery', "I've got your food order. Would you like anything to drink?")

    if slots.get('DrinkQuery') and slots['DrinkQuery'].get('value'):
        parsed_order = json.loads(session_attrs.get('parsedOrder', '{}'))
        order_items = parsed_order.get('order_items', [])
        drink_text = slots['DrinkQuery']['value']['interpretedValue']
        _, menu_lookup, embeddings_cache = get_menu()
        best_key, _ = _fuzzy_find(_normalize_name(drink_text), menu_lookup, embeddings_cache)
        if best_key:
            menu_entry = menu_lookup[best_key]
            order_items.append({"item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": best_key, "quantity": 1, "options": {}, "category": menu_entry.get('category')})
        session_attrs['parsedOrder'] = json.dumps({'order_items': order_items}, cls=DecimalEncoder)
    
    if session_attrs.get('parsedOrder'):
        final_order_items = json.loads(session_attrs['parsedOrder']).get('order_items', [])
        summary_parts = []
        for item in final_order_items:
            options_str = ""
            if item.get('options'): options_str = " (" + ", ".join(f"{v}" for v in item['options'].values()) + ")"
            summary_parts.append(f"{item['quantity']} {item['item_name']}{options_str}")
        summary = "Okay, I have: " + ", ".join(summary_parts) + ". Is that correct?"
        return confirm_intent(event, session_attrs, summary)
    return delegate(event, session_attrs)

def fulfill_order(event):
    try:
        session_attrs = event['sessionState'].get('sessionAttributes', {})
        final_order_str = session_attrs.get('parsedOrder', '{}')
        final_order = json.loads(final_order_str)
        summary = "Thank you! Your order for " + ", ".join([f"{item['quantity']} {item['item_name']}" for item in final_order.get('order_items', [])]) + " has been placed."
        return close_dialog(event, session_attrs, 'Fulfilled', {'contentType': 'PlainText', 'content': summary})
    except Exception as e:
        print(f"Error fulfilling order: {e}"); traceback.print_exc()
        return close_dialog(event, event['sessionState'].get('sessionAttributes', {}), 'Failed', {'contentType': 'PlainText', 'content': "I encountered an error while finalizing your order."})

def _extract_json_from_text(text):
    if not text: return None
    try: json.loads(text); return text
    except json.JSONDecodeError: pass
    try:
        start = text.find('{')
        if start == -1: return None
        brace_count = 0
        for i in range(start, len(text)):
            if text[i] == '{': brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_str = text[start:i+1]; json.loads(json_str); return json_str
        return None
    except Exception: return None

def invoke_openrouter_parser(user_text):
    system = ("You are a strict JSON parser. Extract items from the user's order and return a single JSON object with key 'order_items'. Each item must have 'item_name', 'quantity', and optional 'options' (an object). If an item has variants (like beef/vegetable gyoza) and the customer specifies it, include it in the item_name.")
    examples = [{"role": "user", "content": "I want two green dragon rolls and one nestea."}, {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "green dragon roll", "quantity": 2}, {"item_name": "nestea", "quantity": 1}]})}, {"role": "user", "content": "One Sashimi, Sushi & Maki Combo B and three seaweed salads."}, {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "Sashimi, Sushi & Maki Combo", "quantity": 1, "options": {"Combo Choice": "B"}}, {"item_name": "Seaweed Salad", "quantity": 3}]})}, {"role": "user", "content": "I'd like beef gyoza and a coke."}, {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "beef gyoza", "quantity": 1}, {"item_name": "coke", "quantity": 1}]})}]
    prompt_user = f'Customer said: "{user_text}". Respond with JSON only.'
    try:
        completion = client.chat.completions.create(model=MODEL_NAME, messages=[{"role": "system", "content": system}, *examples, {"role": "user", "content": prompt_user}], stream=False)
        response_text = completion.choices[0].message.content
        json_str = _extract_json_from_text(response_text)
        if json_str:
            parsed_json = json.loads(json_str)
            # --- MODIFIED PART ---
            # Ensure the response always has the correct structure
            if 'order_items' not in parsed_json or not isinstance(parsed_json.get('order_items'), list):
                return {'order_items': []}
            return parsed_json
        return {'order_items': []} # Return empty structure if no JSON
    except Exception as e:
        print(f"Error calling OpenRouter: {e}"); traceback.print_exc()
        return {'order_items': []} # Return empty structure on error
    
def elicit_slot(event, session_attrs, slot_to_elicit, message_content, reset=False):
    intent = event['sessionState']['intent']
    if reset:
        intent['slots'] = {"OrderQuery": None, "DrinkQuery": None, "OptionChoice": None}
        session_attrs = {}
    response = {'sessionState': {'dialogAction': {'type': 'ElicitSlot', 'slotToElicit': slot_to_elicit}, 'intent': intent, 'sessionAttributes': session_attrs}, 'messages': [{'contentType': 'PlainText', 'content': message_content}]}
    print(f"RESPONSE to Lex: {json.dumps(response)}") # <-- ADDED
    return response

def confirm_intent(event, session_attrs, message_content):
    response = {'sessionState': {'dialogAction': {'type': 'ConfirmIntent'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}, 'messages': [{'contentType': 'PlainText', 'content': message_content}]}
    print(f"RESPONSE to Lex: {json.dumps(response)}") # <-- ADDED
    return response

def delegate(event, session_attrs):
    response = {'sessionState': {'dialogAction': {'type': 'Delegate'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}}
    print(f"RESPONSE to Lex: {json.dumps(response)}") # <-- ADDED
    return response

def close_dialog(event, session_attrs, fulfillment_state, message):
    event['sessionState']['intent']['state'] = fulfillment_state
    response = {'sessionState': {'dialogAction': {'type': 'Close'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}, 'messages': [message]}
    print(f"RESPONSE to Lex: {json.dumps(response)}") # <-- ADDED
    return response