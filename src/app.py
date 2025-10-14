# app.py
import json
import boto3
import os
import decimal
import time
import re
from openai import OpenAI

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

# Global caches (remain the same)
_menu_cache_timestamp = 0
_menu_cache_ttl_seconds = int(os.environ.get("MENU_CACHE_TTL", 300))
_menu_raw = None
_menu_lookup = None
_menu_embeddings_cache = None

# --- (The following helper functions do not need changes) ---
class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return float(o)
        return super(DecimalEncoder, self).default(o)

def _normalize_name(s):
    if not isinstance(s, str):
        return ""
    return re.sub(r'\s+', ' ', s.strip().lower())

def _build_menu_lookup(items):
    # This function does not need changes
    lookup = {}
    for item in items:
        raw_name = item.get('ItemName', '')
        if not raw_name:
            continue
        normalized = _normalize_name(raw_name)
        options_struct = {}
        raw_options_list = item.get('Options', [])
        for opt in raw_options_list:
            opt_m = opt.get('M', opt)
            opt_name_raw = opt_m.get('name', {}).get('S') if isinstance(opt_m.get('name'), dict) else opt_m.get('name')
            if not opt_name_raw:
                continue
            opt_name = _normalize_name(opt_name_raw)
            choices = []
            items_list = opt_m.get('items', {}).get('L', []) if isinstance(opt_m.get('items'), dict) else opt_m.get('items', [])
            for c in items_list:
                c_m = c.get('M', c)
                choice_name_raw = c_m.get('name', {}).get('S') if isinstance(c_m.get('name'), dict) else c_m.get('name')
                if choice_name_raw:
                    choices.append(_normalize_name(choice_name_raw))
            options_struct[opt_name] = {
                "raw_name": opt_name_raw,
                "choices": choices,
                "required": bool(opt_m.get('required', {}).get('BOOL')) if isinstance(opt_m.get('required'), dict) else bool(opt_m.get('required'))
            }
        lookup[normalized] = {
            "raw_item": item,
            "normalized_name": normalized,
            "options": options_struct,
            "category": item.get('Category'),
            "price": item.get('Price'),
            "item_number": item.get('ItemNumber')
        }
    return lookup

def get_menu(force_refresh=False):
    # This function's logic for caching embeddings is still valid and does not need changes
    global _menu_cache_timestamp, _menu_raw, _menu_lookup, _menu_embeddings_cache
    now = int(time.time())
    if force_refresh or _menu_raw is None or (now - _menu_cache_timestamp) > _menu_cache_ttl_seconds:
        print("Refreshing menu cache and embeddings from DynamoDB.")
        resp = menu_table.scan()
        items = resp.get('Items', [])
        
        _menu_raw = items
        _menu_lookup = _build_menu_lookup(items)
        _menu_cache_timestamp = now

        embeddings = []
        for item in items:
            embedding_decimals = item.get('ItemEmbedding')
            if embedding_decimals:
                embedding_floats = np.array([float(d) for d in embedding_decimals])
                embeddings.append({
                    "normalized_key": _normalize_name(item.get('ItemName', '')),
                    "embedding": embedding_floats
                })
        _menu_embeddings_cache = embeddings
    else:
        print("Menu cache hit.")
    return _menu_raw, _menu_lookup, _menu_embeddings_cache

# --- MODIFIED _fuzzy_find to use Google Gemini API ---
def _fuzzy_find(normalized_name, menu_lookup, embeddings_cache, cutoff=0.8):
    if not normalized_name:
        return None, 0.0

    if normalized_name in menu_lookup:
        return normalized_name, 1.0

    # 1. Generate embedding for the user's query by calling the Google API
    try:
        result = genai.embed_content(
            model=GEMINI_EMBEDDING_MODEL,
            content=normalized_name,
            task_type="RETRIEVAL_QUERY" # Use QUERY for user searches
        )
        query_embedding = result['embedding']
    except Exception as e:
        print(f"Error getting embedding from Google API: {e}")
        return None, 0.0

    best_score = -1
    best_match_key = None

    # 2. Find the best match using the same NumPy calculation
    for item_embedding in embeddings_cache:
        v1 = query_embedding
        v2 = item_embedding['embedding']
        
        dot_product = np.dot(v1, v2)
        norm_v1 = np.linalg.norm(v1)
        norm_v2 = np.linalg.norm(v2)
        
        if norm_v1 == 0 or norm_v2 == 0:
            similarity = 0.0
        else:
            similarity = dot_product / (norm_v1 * norm_v2)
        
        if similarity > best_score:
            best_score = similarity
            best_match_key = item_embedding['normalized_key']

    if best_score >= cutoff:
        return best_match_key, best_score
    
    return None, 0.0
# ---------------------------------------------

# --- (The rest of your code remains the same) ---
def lambda_handler(event, context):
    print("Event received")
    print(json.dumps(event))
    invocation_source = event.get('invocationSource')
    if invocation_source == 'DialogCodeHook':
        return handle_dialog(event)
    elif invocation_source == 'FulfillmentCodeHook':
        return fulfill_order(event)
    return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "Sorry, I couldn't handle your request."})

def handle_dialog(event):
    intent = event['sessionState']['intent']
    slots = intent.get('slots', {})
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}
    confirmation_state = intent.get('confirmationState')

    if confirmation_state == 'Confirmed':
        return delegate(event, session_attrs)
    if confirmation_state == 'Denied':
        return elicit_slot(event, 'OrderQuery', "Okay — let's start over. What would you like to order?", reset=True)

    if session_attrs.get('currentItemToConfigure'):
        session_attrs.pop('currentItemToConfigure', None)
        return elicit_slot(event, 'DrinkQuery', "Great — anything to drink with that?")

    if not slots.get('OrderQuery'):
        return elicit_slot(event, 'OrderQuery', "Sure — what would you like to order?")

    if slots.get('OrderQuery') and not session_attrs.get('initialParseComplete'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']

        print(f"Invoking LLM parser with text: '{raw_order_text}'")

        try:
            parsed_result = invoke_openrouter_parser(raw_order_text)
            order_items = parsed_result.get('order_items', [])
            normalized_items = []
            _, menu_lookup, embeddings_cache = get_menu()

            for it in order_items:
                parsed_name = it.get('item_name', '')
                quantity = int(it.get('quantity', 1))
                options = it.get('options', {})
                norm = _normalize_name(parsed_name)
                best_key, score = _fuzzy_find(norm, menu_lookup, embeddings_cache)
                
                if best_key:
                    menu_entry = menu_lookup[best_key]
                    validated_options = {}
                    for opt_key_raw, opt_val_raw in (options.items() if isinstance(options, dict) else []):
                        opt_key = _normalize_name(opt_key_raw)
                        opt_val = _normalize_name(str(opt_val_raw))
                        menu_opt = menu_entry['options'].get(opt_key)
                        if menu_opt and opt_val in menu_opt['choices']:
                            validated_options[menu_opt['raw_name']] = opt_val_raw
                        else:
                            validated_options[opt_key_raw] = opt_val_raw
                    normalized_items.append({
                        "item_name": menu_entry['raw_item'].get('ItemName'),
                        "normalized_key": best_key, "quantity": quantity,
                        "options": validated_options, "category": menu_entry.get('category'),
                        "price": menu_entry.get('price'), "item_number": menu_entry.get('item_number')
                    })
                else:
                    normalized_items.append({
                        "item_name": parsed_name, "normalized_key": None,
                        "quantity": quantity, "options": options
                    })

            session_attrs['parsedOrder'] = json.dumps({"order_items": normalized_items}, cls=DecimalEncoder)
            session_attrs['initialParseComplete'] = "true"

            if not any(item.get('normalized_key') for item in normalized_items):
                 return elicit_slot(event, 'OrderQuery', f"I'm sorry, I couldn't find any of those items on the menu. Could you please tell me your order again?")

            unmatched = [i for i in normalized_items if not i.get('normalized_key')]
            if unmatched:
                return elicit_slot(event, 'OrderQuery', f"I couldn't find '{unmatched[0]['item_name']}' on the menu. Could you clarify that part of your order?")

            for ni in normalized_items:
                if ni.get('normalized_key'):
                    entry = menu_lookup[ni['normalized_key']]
                    for opt_key_norm, opt_meta in entry['options'].items():
                        if opt_meta.get('required') and (not ni['options'] or opt_meta.get('raw_name') not in ni['options']):
                            session_attrs['currentItemToConfigure'] = json.dumps(entry['raw_item'], cls=DecimalEncoder)
                            option_name = opt_meta.get('raw_name')
                            choices_text = ", ".join(opt_meta.get('choices', []))
                            return elicit_slot(event, 'OptionChoice', f"For {ni['item_name']}, which {option_name} would you like? Choices: {choices_text}")

            has_food = any(i.get('category') and 'drink' not in str(i.get('category','')).lower() for i in normalized_items)
            has_drink = any(i.get('category') and 'drink' in str(i.get('category','')).lower() for i in normalized_items)

            if has_food and has_drink:
                summary = "Okay — I have: " + ", ".join([f"{i['quantity']} {i['item_name']}" for i in normalized_items]) + ". Is that correct?"
                return confirm_intent(event, summary)
            elif has_food and not has_drink:
                return elicit_slot(event, 'DrinkQuery', "I've got your food order. Would you like anything to drink?")
            elif has_drink and not has_food:
                return elicit_slot(event, 'OrderQuery', f"Okay, I have your drinks. What would you like to eat?")

        except Exception as e:
            print("Error during parsing:", e)
            return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I had trouble understanding your order. Could you try again?"})

    if slots.get('DrinkQuery'):
        parsed_order = json.loads(session_attrs.get('parsedOrder', '{}'))
        order_items = parsed_order.get('order_items', [])
        drink_text = slots['DrinkQuery']['value']['interpretedValue']
        _, menu_lookup, embeddings_cache = get_menu()
        best_key, score = _fuzzy_find(_normalize_name(drink_text), menu_lookup, embeddings_cache)
        if best_key:
            menu_entry = menu_lookup[best_key]
            order_items.append({
                "item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": best_key,
                "quantity": 1, "options": {}, "category": menu_entry.get('category')
            })
        session_attrs['parsedOrder'] = json.dumps({'order_items': order_items}, cls=DecimalEncoder)
        summary = "Okay, I have: " + ", ".join([f"{item['quantity']} {item['item_name']}" for item in order_items]) + ". Is that correct?"
        return confirm_intent(event, summary)

    return delegate(event, session_attrs)

def fulfill_order(event):
    try:
        session_attrs = event['sessionState'].get('sessionAttributes', {})
        final_order_str = session_attrs.get('parsedOrder', '{}')
        final_order = json.loads(final_order_str)
        summary = "Thank you! Your order for " + ", ".join([f"{item['quantity']} {item['item_name']}" for item in final_order.get('order_items', [])]) + " has been placed."
        return close_dialog(event, 'Fulfilled', {'contentType': 'PlainText', 'content': summary})
    except Exception as e:
        print("Error fulfilling order:", e)
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I encountered an error while finalizing your order."})

def _extract_json_from_text(text):
    try:
        start = text.find('{')
        if start == -1: return None
        brace = 0
        for i in range(start, len(text)):
            if text[i] == '{': brace += 1
            elif text[i] == '}':
                brace -= 1
                if brace == 0: return text[start:i+1]
        return None
    except Exception:
        return None

def invoke_openrouter_parser(user_text):
    system = "You are a strict JSON parser. Extract items from the user's order and return a single JSON object with key 'order_items'. Each item must have 'item_name', 'quantity', and optional 'options' (an object)."
    examples = [
        {"role": "user", "content": "I want two green dragon rolls and one nestea."},
        {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "green dragon roll", "quantity": 2}, {"item_name": "nestea", "quantity": 1}]})},
        {"role": "user", "content": "One Sashimi, Sushi & Maki Combo B and three seaweed salads."},
        {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "Sashimi, Sushi & Maki Combo", "quantity": 1, "options": {"Combo Choice": "B"}}, {"item_name": "Seaweed Salad", "quantity": 3}]})}
    ]
    prompt_user = f'Customer said: "{user_text}". Respond with JSON only.'
    try:
        completion = client.chat.completions.create(model=MODEL_NAME, messages=[{"role": "system", "content": system}, *examples, {"role": "user", "content": prompt_user}])
        response_text = completion.choices[0].message.content
        if isinstance(response_text, str):
            js_str = _extract_json_from_text(response_text)
            return json.loads(js_str) if js_str else {}
        return response_text if isinstance(response_text, dict) else {}
    except Exception as e:
        print("Error calling OpenRouter:", e)
        return {}

def elicit_slot(event, slot_to_elicit, message_content, reset=False):
    intent = event['sessionState']['intent']
    session_attrs = event['sessionState'].get('sessionAttributes', {})
    if reset:
        intent['slots'] = {"OrderQuery": None, "DrinkQuery": None, "OptionChoice": None}
        session_attrs = {}
    return {'sessionState': {'dialogAction': {'type': 'ElicitSlot', 'slotToElicit': slot_to_elicit}, 'intent': intent, 'sessionAttributes': session_attrs}, 'messages': [{'contentType': 'PlainText', 'content': message_content}]}

def confirm_intent(event, message_content):
    return {'sessionState': {'dialogAction': {'type': 'ConfirmIntent'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': event['sessionState'].get('sessionAttributes', {})}, 'messages': [{'contentType': 'PlainText', 'content': message_content}]}

def delegate(event, session_attrs):
    return {'sessionState': {'dialogAction': {'type': 'Delegate'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}}

def close_dialog(event, fulfillment_state, message):
    event['sessionState']['intent']['state'] = fulfillment_state
    return {'sessionState': {'dialogAction': {'type': 'Close'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': event['sessionState'].get('sessionAttributes', {})}, 'messages': [message]}