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
    
    DynamoDB types:
    - S: String
    - N: Number
    - L: List
    - M: Map
    - BOOL: Boolean
    """
    if isinstance(value, dict):
        # Check if it's a DynamoDB type descriptor
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
            # Regular dict, unwrap each value
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
        # First, ensure the item is fully unwrapped
        unwrapped_item = _unwrap_dynamodb_value(item)
        
        raw_name = unwrapped_item.get('ItemName', '')
        if not raw_name:
            continue
        
        normalized = _normalize_name(raw_name)
        options_struct = {}
        
        # Get the Options field (should be a list now)
        raw_options_list = unwrapped_item.get('Options', [])
        
        if not isinstance(raw_options_list, list):
            print(f"WARNING: Options for '{raw_name}' is not a list: {type(raw_options_list)}")
            raw_options_list = []
        
        for opt_idx, opt in enumerate(raw_options_list):
            if not isinstance(opt, dict):
                print(f"ERROR: Option at index {opt_idx} for '{raw_name}' is not a dict: {type(opt)}")
                continue
            
            # Option name
            opt_name_raw = opt.get('name', '')
            if not opt_name_raw:
                print(f"WARNING: Option at index {opt_idx} for '{raw_name}' has no name")
                continue
            
            opt_name = _normalize_name(opt_name_raw)
            
            # Option choices
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
            
            # Get required flag
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
            
            if items:
                first_item = items[0]
                print(f"First item keys: {list(first_item.keys())[:5]}...")
                print(f"First item ItemName type: {type(first_item.get('ItemName'))}")
            
            _menu_raw = items
            _menu_lookup = _build_menu_lookup(items)
            _menu_cache_timestamp = now

            # Build embeddings cache
            embeddings = []
            for item in items:
                unwrapped_item = _unwrap_dynamodb_value(item)
                
                embedding_value = unwrapped_item.get('ItemEmbedding')
                
                if embedding_value:
                    if isinstance(embedding_value, list):
                        embedding_floats = np.array([float(x) for x in embedding_value])
                    else:
                        print(f"WARNING: ItemEmbedding for '{unwrapped_item.get('ItemName')}' is not a list")
                        continue
                    
                    embeddings.append({
                        "normalized_key": _normalize_name(unwrapped_item.get('ItemName', '')),
                        "embedding": embedding_floats
                    })
            
            _menu_embeddings_cache = embeddings
            print(f"Successfully loaded {len(_menu_embeddings_cache)} embeddings into cache")
            print(f"Built lookup for {len(_menu_lookup)} menu items")
            
        except Exception as e:
            print(f"ERROR loading menu from DynamoDB: {e}")
            traceback.print_exc()
            raise
    else:
        print("Menu cache hit.")
    
    return _menu_raw, _menu_lookup, _menu_embeddings_cache

def _fuzzy_find(normalized_name, menu_lookup, embeddings_cache, cutoff=0.6):
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
    For example: "beef gyoza" includes "beef" which is an option for "Gyoza"
    
    Returns: dict of {option_raw_name: detected_choice_value}
    """
    detected_options = {}
    normalized_parsed = _normalize_name(parsed_name)
    
    # Split the customer's input into words
    customer_words = normalized_parsed.split()
    
    # Check each option group in the menu item
    for opt_key_norm, opt_meta in menu_entry['options'].items():
        option_raw_name = opt_meta['raw_name']
        
        # Check each choice in this option group
        for choice_normalized in opt_meta.get('choices', []):
            # Check if this choice appears as a word in what customer said
            if choice_normalized in customer_words:
                # Found a match!
                detected_options[option_raw_name] = choice_normalized
                print(f"✓ DETECTED: '{parsed_name}' contains option '{choice_normalized}' for '{option_raw_name}'")
                break  # Found this option, move to next option group
    
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

    if confirmation_state == 'Confirmed':
        return delegate(event, session_attrs)
    if confirmation_state == 'Denied':
        return elicit_slot(event, 'OrderQuery', "Okay — let's start over. What would you like to order?", reset=True)

    if session_attrs.get('currentItemToConfigure'):
        session_attrs.pop('currentItemToConfigure', None)
        return elicit_slot(event, 'DrinkQuery', "Great — anything to drink with that?")

    if not slots.get('OrderQuery'):
        return elicit_slot(event, 'OrderQuery', "Sure — what would you like to order?")

    # CRITICAL: Only parse the order if we haven't done it yet
    if slots.get('OrderQuery') and not session_attrs.get('initialParseComplete'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']
        
        print(f"Invoking LLM parser with text: '{raw_order_text}'")
        
        try:
            parsed_result = invoke_openrouter_parser(raw_order_text)

            print(f"DEBUG: parsed_result type: {type(parsed_result)}")
            print(f"LLM parser returned: {json.dumps(parsed_result)}")

            # Ensure parsed_result is a dict
            if not isinstance(parsed_result, dict):
                print(f"ERROR: parsed_result is not a dict, it's {type(parsed_result)}")
                return elicit_slot(event, 'OrderQuery', "I had trouble understanding your order. Could you try again?")

            order_items = parsed_result.get('order_items', [])
            
            # Ensure order_items is a list
            if not isinstance(order_items, list):
                print(f"ERROR: order_items is not a list, it's {type(order_items)}")
                return elicit_slot(event, 'OrderQuery', "I had trouble understanding your order. Could you try again?")
            
            print(f"DEBUG: order_items type: {type(order_items)}, count: {len(order_items)}")
            
            normalized_items = []
            _, menu_lookup, embeddings_cache = get_menu()

            for idx, it in enumerate(order_items):
                print(f"\n=== Processing item {idx} ===")
                print(f"Item type: {type(it)}, value: {it}")
                
                # CRITICAL: Ensure each item is a dictionary
                if not isinstance(it, dict):
                    print(f"ERROR: Item at index {idx} is not a dict, it's {type(it)}: {it}")
                    continue
                
                parsed_name = it.get('item_name', '')
                if not parsed_name:
                    print(f"WARNING: Item at index {idx} has no item_name")
                    continue
                
                quantity = int(it.get('quantity', 1))
                
                # CRITICAL: Ensure options is always a dict
                options = it.get('options')
                if options is None:
                    options = {}
                elif not isinstance(options, dict):
                    print(f"WARNING: options for '{parsed_name}' is not a dict, it's {type(options)}: {options}")
                    options = {}

                print(f"Parsed: name='{parsed_name}', quantity={quantity}, options={options}")

                norm = _normalize_name(parsed_name)
                best_key, score = _fuzzy_find(norm, menu_lookup, embeddings_cache)
                
                if best_key:
                    print(f"Found menu match: '{best_key}'")
                    menu_entry = menu_lookup[best_key]
                    
                    # Auto-detect options in item name (e.g., "beef" in "beef gyoza")
                    detected_options = _check_if_option_in_item_name(parsed_name, menu_entry)
                    
                    # Merge auto-detected and LLM-provided options
                    validated_options = {}
                    
                    # Start with auto-detected options
                    for opt_name, opt_value in detected_options.items():
                        validated_options[opt_name] = opt_value
                    
                    # Add/override with LLM-provided options
                    for opt_key_raw, opt_val_raw in options.items():
                        opt_key = _normalize_name(opt_key_raw)
                        opt_val = _normalize_name(str(opt_val_raw))
                        menu_opt = menu_entry['options'].get(opt_key)
                        if menu_opt and opt_val in menu_opt['choices']:
                            validated_options[menu_opt['raw_name']] = opt_val_raw
                        else:
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
                    print(f"No menu match found for '{parsed_name}'")
                    normalized_items.append({
                        "item_name": parsed_name,
                        "normalized_key": None,
                        "quantity": quantity,
                        "options": options
                    })
            
            print(f"\n=== Normalized {len(normalized_items)} items ===")
            print(f"Normalized items: {json.dumps(normalized_items, cls=DecimalEncoder)}")

            session_attrs['parsedOrder'] = json.dumps({"order_items": normalized_items}, cls=DecimalEncoder)
            session_attrs['initialParseComplete'] = "true"

            if not any(item.get('normalized_key') for item in normalized_items):
                 return elicit_slot(event, 'OrderQuery', "I'm sorry, I couldn't find any of those items on the menu. Could you please tell me your order again?")

            unmatched = [i for i in normalized_items if not i.get('normalized_key')]
            if unmatched:
                return elicit_slot(event, 'OrderQuery', f"I couldn't find '{unmatched[0]['item_name']}' on the menu. Could you clarify that part of your order?")

            # Check for missing required options
            for ni in normalized_items:
                if ni.get('normalized_key'):
                    entry = menu_lookup[ni['normalized_key']]
                    for opt_key_norm, opt_meta in entry['options'].items():
                        if opt_meta.get('required'):
                            # Check if this required option was provided
                            option_provided = False
                            if ni['options']:
                                for provided_opt_key, provided_opt_val in ni['options'].items():
                                    if _normalize_name(provided_opt_key) == opt_key_norm or provided_opt_key == opt_meta.get('raw_name'):
                                        option_provided = True
                                        print(f"✓ Required option '{opt_meta.get('raw_name')}' satisfied with '{provided_opt_val}'")
                                        break
                            
                            if not option_provided:
                                session_attrs['currentItemToConfigure'] = json.dumps(ni, cls=DecimalEncoder)
                                option_name = opt_meta.get('raw_name')
                                choices_text = ", ".join(opt_meta.get('choices', []))
                                return elicit_slot(event, 'OptionChoice', f"For your {ni['item_name']}, which {option_name} would you like? Choices: {choices_text}")

            has_food = any(i.get('category') and 'drink' not in str(i.get('category','')).lower() for i in normalized_items)
            has_drink = any(i.get('category') and 'drink' in str(i.get('category','')).lower() for i in normalized_items)

            if has_food and has_drink:
                summary = "Okay — I have: " + ", ".join([f"{i['quantity']} {i['item_name']}" for i in normalized_items]) + ". Is that correct?"
                return confirm_intent(event, summary)
            elif has_food and not has_drink:
                return elicit_slot(event, 'DrinkQuery', "I've got your food order. Would you like anything to drink?")
            elif has_drink and not has_food:
                return elicit_slot(event, 'OrderQuery', "Okay, I have your drinks. What would you like to eat?")

        except Exception as e:
            print(f"Error during parsing: {e}")
            traceback.print_exc()
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
                "item_name": menu_entry['raw_item'].get('ItemName'),
                "normalized_key": best_key,
                "quantity": 1,
                "options": {},
                "category": menu_entry.get('category')
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
        traceback.print_exc()
        return close_dialog(event, 'Failed', {'contentType': 'PlainText', 'content': "I encountered an error while finalizing your order."})

def _extract_json_from_text(text):
    """
    Extract JSON object from text that might have extra content.
    Returns the extracted JSON string or None.
    """
    if not text:
        return None
    
    try:
        # First, try to parse the entire text as JSON
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    
    # If that fails, try to extract JSON object
    try:
        start = text.find('{')
        if start == -1:
            return None
        
        brace_count = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_str = text[start:i+1]
                    # Validate it's actually valid JSON before returning
                    json.loads(json_str)
                    return json_str
        
        return None
    except Exception as e:
        print(f"Error extracting JSON: {e}")
        return None

def invoke_openrouter_parser(user_text):
    """
    Parse user's food order text into structured JSON.
    Returns a dict with 'order_items' list, or empty dict on error.
    """
    system = (
        "You are a strict JSON parser. Extract items from the user's order and return "
        "a single JSON object with key 'order_items'. Each item must have 'item_name', "
        "'quantity', and optional 'options' (an object). "
        "IMPORTANT: If an item has variants (like beef/vegetable gyoza), and the customer "
        "specifies the variant (e.g., 'beef gyoza'), include it in the item_name."
    )
    
    examples = [
        {
            "role": "user",
            "content": "I want two green dragon rolls and one nestea."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "order_items": [
                    {"item_name": "green dragon roll", "quantity": 2},
                    {"item_name": "nestea", "quantity": 1}
                ]
            })
        },
        {
            "role": "user",
            "content": "One Sashimi, Sushi & Maki Combo B and three seaweed salads."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "order_items": [
                    {
                        "item_name": "Sashimi, Sushi & Maki Combo",
                        "quantity": 1,
                        "options": {"Combo Choice": "B"}
                    },
                    {
                        "item_name": "Seaweed Salad",
                        "quantity": 3
                    }
                ]
            })
        },
        {
            "role": "user",
            "content": "I'd like beef gyoza and a coke."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "order_items": [
                    {"item_name": "beef gyoza", "quantity": 1},
                    {"item_name": "coke", "quantity": 1}
                ]
            })
        }
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
        
        print(f"Raw response from LLM API: {response_text}")
        print(f"Response type: {type(response_text)}")

        # Handle the response
        if isinstance(response_text, dict):
            return response_text
        
        if isinstance(response_text, str):
            # Extract JSON from the string
            json_str = _extract_json_from_text(response_text)
            
            if json_str:
                parsed = json.loads(json_str)
                print(f"Successfully parsed JSON")
                
                # Validate structure
                if not isinstance(parsed, dict):
                    print(f"ERROR: Parsed JSON is not a dict, it's {type(parsed)}")
                    return {}
                
                if 'order_items' not in parsed:
                    print("ERROR: Parsed JSON missing 'order_items' key")
                    return {}
                
                if not isinstance(parsed['order_items'], list):
                    print(f"ERROR: 'order_items' is not a list, it's {type(parsed['order_items'])}")
                    return {}
                
                # Validate each item in order_items
                for idx, item in enumerate(parsed['order_items']):
                    if not isinstance(item, dict):
                        print(f"ERROR: Item at index {idx} is not a dict: {type(item)} = {item}")
                        parsed['order_items'] = [i for i in parsed['order_items'] if isinstance(i, dict)]
                        break
                
                return parsed
            else:
                print("ERROR: Could not extract valid JSON from response")
                return {}
        
        print(f"ERROR: Unexpected response type: {type(response_text)}")
        return {}
        
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        traceback.print_exc()
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
            'dialogAction': {
                'type': 'ElicitSlot',
                'slotToElicit': slot_to_elicit
            },
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