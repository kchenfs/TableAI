# app.py
import json
import boto3
import os
import decimal
import time
import re
from openai import OpenAI
import traceback

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
    if not isinstance(s, str):
        return ""
    return re.sub(r'\s+', ' ', s.strip().lower())

def _unwrap_dynamodb_value(value):
    """
    Recursively unwrap DynamoDB type descriptors.
    """
    if isinstance(value, dict):
        if 'S' in value:
            return value['S']
        elif 'N' in value:
            return float(value['N'])
        elif 'BOOL' in value:
            return value['BOOL']
        elif 'L' in value:
            return [_unwrap_dynamodb_value(item) for item in value['L']]
        elif 'M' in value:
            return {k: _unwrap_dynamodb_value(v) for k, v in value['M'].items()}
        else:
            return {k: _unwrap_dynamodb_value(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_unwrap_dynamodb_value(item) for item in value]
    else:
        return value

def _build_menu_lookup(items):
    """
    Build a lookup dict from menu items with proper DynamoDB unwrapping.
    """
    lookup = {}
    
    for item in items:
        unwrapped_item = _unwrap_dynamodb_value(item)
        
        raw_name = unwrapped_item.get('ItemName', '')
        if not raw_name:
            continue
        
        normalized = _normalize_name(raw_name)
        options_struct = {}
        
        raw_options_list = unwrapped_item.get('Options', [])
        
        if not isinstance(raw_options_list, list):
            print(f"WARNING: Options for '{raw_name}' is not a list: {type(raw_options_list)}")
            raw_options_list = []
        
        for opt_idx, opt in enumerate(raw_options_list):
            if not isinstance(opt, dict):
                print(f"ERROR: Option at index {opt_idx} for '{raw_name}' is not a dict: {type(opt)}")
                continue
            
            opt_name_raw = opt.get('name', '')
            if not opt_name_raw:
                print(f"WARNING: Option at index {opt_idx} for '{raw_name}' has no name")
                continue
            
            opt_name = _normalize_name(opt_name_raw)
            
            choices = []
            items_list = opt.get('items', [])
            
            if not isinstance(items_list, list):
                print(f"ERROR: items for option '{opt_name_raw}' is not a list: {type(items_list)}")
                items_list = []
            
            for choice_idx, choice_item in enumerate(items_list):
                if not isinstance(choice_item, dict):
                    print(f"ERROR: Choice at index {choice_idx} is not a dict: {type(choice_item)}")
                    continue
                
                choice_name_raw = choice_item.get('name', '')
                if choice_name_raw:
                    choices.append(_normalize_name(choice_name_raw))
            
            required = opt.get('required', False)
            
            options_struct[opt_name] = {
                "raw_name": opt_name_raw,
                "choices": choices,
                "required": bool(required)
            }
        
        lookup[normalized] = {
            "raw_item": unwrapped_item,
            "normalized_name": normalized,
            "options": options_struct,
            "category": unwrapped_item.get('Category'),
            "price": unwrapped_item.get('Price'),
            "item_number": unwrapped_item.get('ItemNumber')
        }
    
    return lookup

def get_menu(force_refresh=False):
    """
    Fetch and cache menu items from DynamoDB.
    """
    global _menu_cache_timestamp, _menu_raw, _menu_lookup, _menu_embeddings_cache
    
    now = int(time.time())
    
    if force_refresh or _menu_raw is None or (now - _menu_cache_timestamp) > _menu_cache_ttl_seconds:
        print("Refreshing menu cache and embeddings from DynamoDB.")
        
        try:
            resp = menu_table.scan()
            items = resp.get('Items', [])
            
            print(f"Found {len(items)} items from DynamoDB.")
            
            _menu_raw = items
            _menu_lookup = _build_menu_lookup(items)
            _menu_cache_timestamp = now

            embeddings = []
            for item in items:
                unwrapped_item = _unwrap_dynamodb_value(item)
                
                embedding_value = unwrapped_item.get('ItemEmbedding')
                
                if embedding_value and isinstance(embedding_value, list):
                    embedding_floats = np.array([float(x) for x in embedding_value])
                    embeddings.append({
                        "normalized_key": _normalize_name(unwrapped_item.get('ItemName', '')),
                        "embedding": embedding_floats
                    })
            
            _menu_embeddings_cache = embeddings
            print(f"Successfully loaded {len(_menu_embeddings_cache)} embeddings into cache")
            
        except Exception as e:
            print(f"ERROR loading menu from DynamoDB: {e}")
            traceback.print_exc()
            raise
    else:
        print("Menu cache hit.")
    
    return _menu_raw, _menu_lookup, _menu_embeddings_cache

def _fuzzy_find(normalized_name, menu_lookup, embeddings_cache, cutoff=0.8):
    if not normalized_name:
        return None, 0.0

    if normalized_name in menu_lookup:
        print(f"Direct match found for '{normalized_name}'")
        return normalized_name, 1.0

    try:
        print(f"Embedding live query text: '{normalized_name}'")
        result = genai.embed_content(
            model=GEMINI_EMBEDDING_MODEL,
            content=normalized_name,
            task_type="RETRIEVAL_QUERY"
        )
        query_embedding = result['embedding']
        print(f"Received vector from Gemini for '{normalized_name}'. Dimensions: {len(query_embedding)}")
    except Exception as e:
        print(f"Error getting embedding from Google API for '{normalized_name}': {e}")
        return None, 0.0

    best_score = -1
    best_match_key = None

    for item_embedding in embeddings_cache:
        v1 = query_embedding
        v2 = item_embedding['embedding']
        dot_product = np.dot(v1, v2)
        norm_v1 = np.linalg.norm(v1)
        norm_v2 = np.linalg.norm(v2)
        similarity = 0.0 if norm_v1 == 0 or norm_v2 == 0 else dot_product / (norm_v1 * norm_v2)
        
        if similarity > best_score:
            best_score = similarity
            best_match_key = item_embedding['normalized_key']

    print(f"Fuzzy find for '{normalized_name}': Best match is '{best_match_key}' with score {best_score:.4f}")

    if best_score >= cutoff:
        return best_match_key, best_score
    
    return None, 0.0

def _check_if_option_in_item_name(parsed_name, menu_entry):
    """
    Check if the customer's item name includes an option choice.
    """
    detected_options = {}
    normalized_parsed = _normalize_name(parsed_name)
    customer_words = set(normalized_parsed.split())
    
    for opt_key_norm, opt_meta in menu_entry['options'].items():
        option_raw_name = opt_meta['raw_name']
        for choice_normalized in opt_meta.get('choices', []):
            if choice_normalized in customer_words:
                detected_options[option_raw_name] = choice_normalized
                print(f"✓ DETECTED: '{parsed_name}' contains option '{choice_normalized}' for '{option_raw_name}'")
                break
    return detected_options

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
    
    # --- FIX: LOGIC TO HANDLE OPTION CHOICE AND PREVENT LOOP ---
    if slots.get('OptionChoice') and slots['OptionChoice'].get('value') and session_attrs.get('currentItemToConfigure'):
        try:
            item_to_configure = json.loads(session_attrs['currentItemToConfigure'])
            choice_text = slots['OptionChoice']['value']['interpretedValue']
            
            _, menu_lookup, _ = get_menu()
            menu_entry = menu_lookup.get(item_to_configure.get('normalized_key'))
            
            if menu_entry:
                for opt_key_norm, opt_meta in menu_entry['options'].items():
                    if _normalize_name(choice_text) in opt_meta['choices']:
                        item_to_configure['options'][opt_meta['raw_name']] = choice_text
                        print(f"Applied choice '{choice_text}' to option '{opt_meta['raw_name']}'")
                        break
            
            parsed_order = json.loads(session_attrs.get('parsedOrder', '{}'))
            order_items = parsed_order.get('order_items', [])
            
            for i, item in enumerate(order_items):
                if item.get('normalized_key') == item_to_configure.get('normalized_key'):
                    order_items[i] = item_to_configure
                    break
            
            session_attrs['parsedOrder'] = json.dumps({'order_items': order_items}, cls=DecimalEncoder)
            session_attrs.pop('currentItemToConfigure', None)
            slots['OptionChoice'] = None
        except Exception as e:
            print(f"ERROR handling OptionChoice: {e}")
            traceback.print_exc()

    if confirmation_state == 'Confirmed':
        return delegate(event, session_attrs)
    if confirmation_state == 'Denied':
        return elicit_slot(event, 'OrderQuery', "Okay — let's start over. What would you like to order?", reset=True)

    if not slots.get('OrderQuery'):
        return elicit_slot(event, 'OrderQuery', "Sure — what would you like to order?")

    if slots.get('OrderQuery') and not session_attrs.get('initialParseComplete'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']
        
        try:
            parsed_result = invoke_openrouter_parser(raw_order_text)
            order_items = parsed_result.get('order_items', [])
            
            if not isinstance(order_items, list):
                return elicit_slot(event, 'OrderQuery', "I had trouble understanding your order. Could you try again?")
            
            normalized_items = []
            _, menu_lookup, embeddings_cache = get_menu()

            for it in order_items:
                if not isinstance(it, dict): continue
                parsed_name = it.get('item_name', '')
                if not parsed_name: continue
                
                quantity = int(it.get('quantity', 1))
                options = it.get('options', {})
                if not isinstance(options, dict): options = {}
                
                # --- FIX: LOWERED FUZZY MATCH CUTOFF ---
                best_key, score = _fuzzy_find(_normalize_name(parsed_name), menu_lookup, embeddings_cache, cutoff=0.6)
                
                if best_key:
                    menu_entry = menu_lookup[best_key]
                    detected_options = _check_if_option_in_item_name(parsed_name, menu_entry)
                    validated_options = {}
                    validated_options.update(detected_options)
                    
                    for opt_key_raw, opt_val_raw in options.items():
                         validated_options[opt_key_raw] = opt_val_raw
                    
                    normalized_items.append({
                        "item_name": menu_entry['raw_item'].get('ItemName'),
                        "normalized_key": best_key,
                        "quantity": quantity,
                        "options": validated_options,
                        "category": menu_entry.get('category'),
                        "price": menu_entry.get('price'),
                        "item_number": menu_entry.get('item_number')
                    })
                else:
                    normalized_items.append({
                        "item_name": parsed_name, "normalized_key": None,
                        "quantity": quantity, "options": options
                    })
            
            session_attrs['parsedOrder'] = json.dumps({"order_items": normalized_items}, cls=DecimalEncoder)
            session_attrs['initialParseComplete'] = "true"

            unmatched = [i for i in normalized_items if not i.get('normalized_key')]
            if unmatched:
                return elicit_slot(event, 'OrderQuery', f"I couldn't find '{unmatched[0]['item_name']}' on the menu. Could you clarify that part of your order?")

            # Check for missing required options
            for ni in normalized_items:
                if ni.get('normalized_key'):
                    entry = menu_lookup[ni['normalized_key']]
                    for opt_key_norm, opt_meta in entry['options'].items():
                        if opt_meta.get('required') and opt_meta.get('raw_name') not in ni.get('options', {}):
                            session_attrs['currentItemToConfigure'] = json.dumps(ni, cls=DecimalEncoder)
                            option_name = opt_meta.get('raw_name')
                            choices_text = ", ".join(opt_meta.get('choices', []))
                            return elicit_slot(event, 'OptionChoice', f"For your {ni['item_name']}, which {option_name} would you like? Choices: {choices_text}")

            has_food = any(i.get('category') and 'drink' not in str(i.get('category','')).lower() for i in normalized_items)
            has_drink = any(i.get('category') and 'drink' in str(i.get('category','')).lower() for i in normalized_items)

            if has_food and not has_drink:
                return elicit_slot(event, 'DrinkQuery', "I've got your food order. Would you like anything to drink?")
            else:
                summary = "Okay — I have: " + ", ".join([f"{i['quantity']} {i['item_name']}" for i in normalized_items]) + ". Is that correct?"
                return confirm_intent(event, summary)

        except Exception as e:
            print(f"Error during parsing: {e}")
            traceback.print_exc()
            return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I had trouble understanding. Could you try again?"})

    # This block now primarily handles drink additions and final confirmation
    current_order_str = session_attrs.get('parsedOrder', '{}')
    current_order = json.loads(current_order_str)
    order_items = current_order.get('order_items', [])
    
    if slots.get('DrinkQuery') and slots['DrinkQuery'].get('value'):
        drink_text = slots['DrinkQuery']['value']['interpretedValue']
        _, menu_lookup, embeddings_cache = get_menu()
        best_key, score = _fuzzy_find(_normalize_name(drink_text), menu_lookup, embeddings_cache, cutoff=0.6)
        if best_key:
            menu_entry = menu_lookup[best_key]
            order_items.append({
                "item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": best_key,
                "quantity": 1, "options": {}, "category": menu_entry.get('category')
            })
        session_attrs['parsedOrder'] = json.dumps({'order_items': order_items}, cls=DecimalEncoder)

    # Check for any remaining missing options before confirming the full order
    if session_attrs.get('initialParseComplete'):
        _, menu_lookup, _ = get_menu()
        for ni in order_items:
            if ni.get('normalized_key'):
                entry = menu_lookup[ni['normalized_key']]
                for opt_key_norm, opt_meta in entry['options'].items():
                    if opt_meta.get('required') and opt_meta.get('raw_name') not in ni.get('options', {}):
                        session_attrs['currentItemToConfigure'] = json.dumps(ni, cls=DecimalEncoder)
                        option_name = opt_meta.get('raw_name')
                        choices_text = ", ".join(opt_meta.get('choices', []))
                        return elicit_slot(event, 'OptionChoice', f"For your {ni['item_name']}, which {option_name} would you like? Choices: {choices_text}")

    summary_items = []
    for item in order_items:
        options_str = ""
        if item.get('options'):
            options_str = " (" + ", ".join(item['options'].values()) + ")"
        summary_items.append(f"{item['quantity']} {item['item_name']}{options_str}")

    summary = "Okay — I have: " + ", ".join(summary_items) + ". Is that correct?"
    return confirm_intent(event, summary)

def fulfill_order(event):
    try:
        session_attrs = event['sessionState'].get('sessionAttributes', {})
        final_order_str = session_attrs.get('parsedOrder', '{}')
        final_order = json.loads(final_order_str)
        
        summary_items = []
        for item in final_order.get('order_items', []):
            options_str = ""
            if item.get('options'):
                options_str = " (" + ", ".join(item['options'].values()) + ")"
            summary_items.append(f"{item['quantity']} {item['item_name']}{options_str}")

        summary = "Thank you! Your order for " + ", ".join(summary_items) + " has been placed."
        return close_dialog(event, 'Fulfilled', {'contentType': 'PlainText', 'content': summary})
    except Exception as e:
        print("Error fulfilling order:", e)
        traceback.print_exc()
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I encountered an error while finalizing your order."})

def _extract_json_from_text(text):
    if not text: return None
    try:
        json.loads(text)
        return text
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
                    json_str = text[start:i+1]
                    json.loads(json_str)
                    return json_str
        return None
    except Exception as e:
        print(f"Error extracting JSON: {e}")
        return None

def invoke_openrouter_parser(user_text):
    system = (
        "You are a strict JSON parser. Extract items from the user's order and return "
        "a single JSON object with key 'order_items'. Each item must have 'item_name', "
        "'quantity', and optional 'options' (an object). "
        "IMPORTANT: If an item has variants (like beef/vegetable gyoza), and the customer "
        "specifies the variant (e.g., 'beef gyoza'), include it in the item_name."
    )
    examples = [
        {"role": "user", "content": "I want two green dragon rolls and one nestea."},
        {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "green dragon roll", "quantity": 2},{"item_name": "nestea", "quantity": 1}]})},
        {"role": "user", "content": "One Sashimi, Sushi & Maki Combo B and three seaweed salads."},
        {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "Sashimi, Sushi & Maki Combo","quantity": 1,"options": {"Combo Choice": "B"}},{"item_name": "Seaweed Salad","quantity": 3}]})},
        {"role": "user", "content": "I'd like beef gyoza and a coke."},
        {"role": "assistant", "content": json.dumps({"order_items": [{"item_name": "beef gyoza", "quantity": 1},{"item_name": "coke", "quantity": 1}]})}
    ]
    prompt_user = f'Customer said: "{user_text}". Respond with JSON only.'
    
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system},
                *examples,
                {"role": "user", "content": prompt_user}
            ],
            stream=False
        )
        response_text = completion.choices[0].message.content
        if isinstance(response_text, str):
            json_str = _extract_json_from_text(response_text)
            if json_str:
                return json.loads(json_str)
        return {}
    except Exception as e:
        print(f"Error calling OpenRouter: {e}")
        traceback.print_exc()
        return {}

def elicit_slot(event, slot_to_elicit, message_content, reset=False):
    intent = event['sessionState']['intent']
    session_attrs = event['sessionState'].get('sessionAttributes', {})
    if reset:
        intent['slots'] = {"OrderQuery": None, "DrinkQuery": None, "OptionChoice": None}
        session_attrs = {}
    return {
        'sessionState': {
            'dialogAction': {'type': 'ElicitSlot', 'slotToElicit': slot_to_elicit},
            'intent': intent,
            'sessionAttributes': session_attrs
        },
        'messages': [{'contentType': 'PlainText', 'content': message_content}]
    }

def confirm_intent(event, message_content):
    return {
        'sessionState': {
            'dialogAction': {'type': 'ConfirmIntent'},
            'intent': event['sessionState']['intent'],
            'sessionAttributes': event['sessionState'].get('sessionAttributes', {})
        },
        'messages': [{'contentType': 'PlainText', 'content': message_content}]
    }

def delegate(event, session_attrs):
    return {
        'sessionState': {
            'dialogAction': {'type': 'Delegate'},
            'intent': event['sessionState']['intent'],
            'sessionAttributes': session_attrs
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