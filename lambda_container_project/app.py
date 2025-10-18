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
# import uuid # You would need this if you implement the order saving logic

# --- MODIFIED IMPORTS for Google Gemini ---
import google.generativeai as genai
import numpy as np
# --- NEW: FAISS library for vector search ---
import faiss

# ----------------------------------------

# Environment variables
MENU_TABLE_NAME = os.environ['MENU_TABLE_NAME']
ORDERS_TABLE_NAME = os.environ['ORDERS_TABLE_NAME']
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL_NAME = os.environ.get("MODEL_NAME", "meta-llama/llama-3.3-70b-instruct:free")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")

# AWS and AI model initialization
dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
menu_table = dynamodb.Table(MENU_TABLE_NAME)
orders_table = dynamodb.Table(ORDERS_TABLE_NAME)
_menu_cache_ttl_seconds = 3600

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    GEMINI_EMBEDDING_MODEL = 'models/embedding-001'
else:
    print("Warning: GOOGLE_API_KEY environment variable not set.")

# Global caches
_menu_cache_timestamp = 0
_menu_raw = None
_menu_lookup = None
_menu_embeddings_cache = None
_rag_index = None
_rag_chunks = None

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

# --- NEW HELPER FUNCTION --- to fix the duplicate option bug permanently
def _normalize_options(detected_options, menu_entry):
    """Maps detected option keys (like 'size') to the official menu option name (like 'Tray Size')."""
    normalized_options = {}
    official_options = menu_entry.get('options', {})

    for detected_key, detected_value in detected_options.items():
        norm_detected_key = _normalize_name(detected_key)
        found_match = False
        for official_key_norm, official_meta in official_options.items():
            # Check if the detected key is the official one or very similar
            if norm_detected_key == official_key_norm or norm_detected_key in official_key_norm:
                normalized_options[official_meta['raw_name']] = detected_value
                found_match = True
                break
        if not found_match:
            # If no match, keep the original but this is less ideal
            normalized_options[detected_key] = detected_value
            
    return normalized_options

def get_rag_answer(event):
    global _rag_index, _rag_chunks
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}
    transcript = event.get('inputTranscript', '')
    print(f"RAG: Getting answer for question: '{transcript}'")

    try:
        if _rag_index is None:
            print("RAG: Loading knowledge base from local container image.")
            _rag_index = faiss.read_index('rag_index.faiss')
            with open('rag_chunks.json', 'r') as f:
                _rag_chunks = json.load(f)
            print("RAG: Index and chunks loaded successfully from local image.")

        query_embedding = genai.embed_content(model=GEMINI_EMBEDDING_MODEL, content=transcript, task_type="RETRIEVAL_QUERY")['embedding']
        distances, indices = _rag_index.search(np.array([query_embedding]), k=3)
        
        retrieved_context = "\n".join([_rag_chunks[i] for i in indices[0]])
        print(f"RAG: Retrieved context:\n{retrieved_context}")

        prompt = f"""
        Based *only* on the context provided below, answer the user's question. If the context does not contain the answer, say you don't have that information.

        Context:
        {retrieved_context}

        Question: {transcript}
        """
        
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        final_answer = completion.choices[0].message.content

    except Exception as e:
        print(f"RAG: Error during RAG pipeline: {e}")
        traceback.print_exc()
        final_answer = "I'm sorry, I encountered an error while looking up that information."

    return close_dialog(event, session_attrs, 'Fulfilled', {'contentType': 'PlainText', 'content': final_answer})

# In app.py

def handle_allergy_intent(event):
    intent = event['sessionState']['intent']
    slots = intent.get('slots', {})
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}

    has_allergy_confirmation = slots.get('hasAllergyConfirmation')
    allergy_details = slots.get('allergyDetails')

    if allergy_details and allergy_details.get('value'):
        specific_allergy = allergy_details['value']['interpretedValue']
        session_attrs['allergyInfo'] = specific_allergy
        print(f"ALLERGY: Captured details: {specific_allergy}")
        return fulfill_order(event, allergy_info=specific_allergy)

    if has_allergy_confirmation and has_allergy_confirmation.get('value') and has_allergy_confirmation['value']['interpretedValue'] == 'Yes':
        print("ALLERGY: User confirmed they have an allergy. Eliciting details.")
        return elicit_slot(event, session_attrs, 'allergyDetails', "Understood. What are your allergies or dietary restrictions?")

    if (has_allergy_confirmation and has_allergy_confirmation.get('value') and has_allergy_confirmation['value']['interpretedValue'] == 'No'):
        print("ALLERGY: User confirmed no allergies.")
        return fulfill_order(event)

    # --- THIS IS THE BLOCK TO CHANGE ---
    transcript = event.get('inputTranscript', '')
    prompt = f"""
    A user was asked if they have allergies. They responded: "{transcript}". 
    Does this response indicate they have an allergy? Respond with a single word: YES, NO, or UNKNOWN.
    """
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME, messages=[{"role": "user", "content": prompt}], temperature=0.0
        )
        llm_decision = completion.choices[0].message.content.strip().upper()
        
        # --- MODIFIED LOGIC ---
        if llm_decision == 'YES':
             print("ALLERGY: LLM determined user has an allergy. Eliciting details.")
             # Instead of fulfilling, ask the user what their allergy is.
             return elicit_slot(event, session_attrs, 'allergyDetails', "Understood. What are your allergies or dietary restrictions?")
        elif llm_decision == 'NO':
             return fulfill_order(event)
             
    except Exception as e:
        print(f"ALLERGY: LLM fallback check failed: {e}")
    # --- END MODIFIED BLOCK ---

    # This is the final fallback if the LLM is confused.
    return elicit_slot(event, session_attrs, 'hasAllergyConfirmation', "I'm sorry, I didn't quite understand. Do you have any allergies? Please answer with yes or no.")
def lambda_handler(event, context):
    print("--- NEW INVOCATION ---")
    print(f"EVENT from Lex: {json.dumps(event)}")
    
    intent_name = event['sessionState']['intent']['name']
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}

    if intent_name == 'FallbackIntent':
        transcript = event.get('inputTranscript', '')
        user_intent = classify_user_intent(transcript)

        if user_intent == 'QUESTION':
            print("HANDLER: Classified as QUESTION. Triggering RAG.")
            return get_rag_answer(event)
        elif user_intent == 'ORDER':
            print("HANDLER: Classified as ORDER. Transforming to OrderFood intent.")
            session_attrs['is_fallback_order'] = 'true'
            event['sessionState']['sessionAttributes'] = session_attrs
            event['sessionState']['intent']['name'] = 'OrderFood'
            if 'slots' not in event['sessionState']['intent']:
                event['sessionState']['intent']['slots'] = {}
            event['sessionState']['intent']['slots']['OrderQuery'] = {'value': {'originalValue': transcript, 'interpretedValue': transcript, 'resolvedValues': []}, 'shape': 'Scalar'}
            return handle_dialog(event)
        elif user_intent == 'MODIFICATION':
            print("HANDLER: Classified as MODIFICATION. Triggering modification logic.")
            return handle_modification_request(event)
        elif user_intent == 'FAREWELL':
            print("HANDLER: Classified as FAREWELL. Closing conversation.")
            message = "You're welcome! Have a great day."
            return close_dialog(event, {}, 'Fulfilled', {'contentType': 'PlainText', 'content': message})
        else:
            print("HANDLER: Classifier was unsure. Responding with help message.")
            message = "I'm sorry, I can only take orders or answer questions about the menu. How can I help?"
            return elicit_slot(event, {}, 'OrderQuery', message, reset=True)

    if intent_name == 'GreetingIntent':
        greetings = ["Hello! I'm ready to take your order. What can I get for you?", "Hi there! What would you like to order today?", "Welcome! Tell me what you'd like to eat."]
        response_message = random.choice(greetings)
        response = {'sessionState': {'dialogAction': {'type': 'ElicitSlot', 'slotToElicit': 'OrderQuery'}, 'intent': {'name': 'OrderFood', 'slots': {'OrderQuery': None, 'DrinkQuery': None, 'OptionChoice': None}, 'state': 'InProgress'}, 'sessionAttributes': {}}, 'messages': [{'contentType': 'PlainText', 'content': response_message}]}
        print(f"RESPONSE to Lex: {json.dumps(response)}")
        return response

    if intent_name == 'AllergyIntent':
        return handle_allergy_intent(event)
        
    if intent_name == 'ModifyOrderIntent':
        return handle_modification_request(event)

    invocation_source = event.get('invocationSource')
    if invocation_source == 'DialogCodeHook':
        return handle_dialog(event)
    elif invocation_source == 'FulfillmentCodeHook':
        return fulfill_order(event)
    
    return close_dialog(event, session_attrs, 'Failed', {'contentType': 'PlainText', 'content': "Sorry, I couldn't handle your request."})
    
def classify_user_intent(transcript):
    print(f"CLASSIFIER: Classifying transcript: '{transcript}'")
    prompt = f"""
    You are an intent classifier for a restaurant bot. Based on the user's input, classify it into one of four categories:
    - 'QUESTION': The user is asking for information (e.g., hours, ingredients, address, recommendations).
    - 'ORDER': The user is stating a food or drink they want to order.
    - 'MODIFICATION': The user wants to change an existing, unconfirmed order (e.g., add, remove, or change an item).
    - 'FAREWELL': The user is saying something to end the conversation (e.g., "thank you", "bye", "that's all").

    Respond with ONLY ONE WORD: QUESTION, ORDER, MODIFICATION, or FAREWELL.
    DO NOT provide any explanations or conversational text. Your entire response must be a single word.

    User input: "{transcript}"
    """
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )
        response = completion.choices[0].message.content.strip().upper()
        if response in ['QUESTION', 'ORDER', 'MODIFICATION', 'FAREWELL']:
            print(f"CLASSIFIER: LLM classified intent as: {response}")
            return response
        else:
            print(f"CLASSIFIER: LLM returned unexpected classification: {response}")
            return None
    except Exception as e:
        print(f"CLASSIFIER: Error during classification: {e}")
        return None
        
def handle_modification_request(event):
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}
    print(f"MODIFICATION: Handling modification request.")

    if 'parsedOrder' not in session_attrs:
        message = "It looks like you haven't placed an order yet. What would you like to get?"
        return elicit_slot(event, session_attrs, 'OrderQuery', message)

    current_order = json.loads(session_attrs['parsedOrder'])
    
    modification_request = event.get('inputTranscript', '')

    try:
        prompt = f"""
        You are a restaurant order modification assistant. Given the current order and a user's request, update the order.
        Respond with a JSON object containing a list of changes. Each change must have an 'action' ('add', 'remove', or 'update'), an 'item_name', and for 'add' actions, a 'quantity'. For 'update' actions, include 'from_item' and 'to_item'.
        
        Current Order: {json.dumps(current_order['order_items'])}
        User Request: "{modification_request}"

        JSON Response:
        """
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        parsed_changes = json.loads(completion.choices[0].message.content)
        print(f"MODIFICATION: Parsed changes from LLM: {parsed_changes}")

        _, menu_lookup, embeddings_cache = get_menu()
        order_items = current_order['order_items']
        
        for change in parsed_changes.get('changes', []):
            action = change.get('action')
            item_name = change.get('item_name', '')

            if action == 'add':
                best_key, _ = _fuzzy_find(_normalize_name(item_name), menu_lookup, embeddings_cache)
                if best_key:
                    menu_entry = menu_lookup[best_key]
                    order_items.append({"item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": best_key, "quantity": change.get('quantity', 1), "options": {}})
            
            elif action == 'remove':
                best_key, _ = _fuzzy_find(_normalize_name(item_name), menu_lookup, embeddings_cache)
                if best_key:
                    order_items = [item for item in order_items if item.get('normalized_key') != best_key]

            elif action == 'update':
                from_item_key, _ = _fuzzy_find(_normalize_name(change.get('from_item')), menu_lookup, embeddings_cache)
                to_item_key, _ = _fuzzy_find(_normalize_name(change.get('to_item')), menu_lookup, embeddings_cache)
                if from_item_key and to_item_key:
                    for i, item in enumerate(order_items):
                        if item.get('normalized_key') == from_item_key:
                            menu_entry = menu_lookup[to_item_key]
                            order_items[i] = {"item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": to_item_key, "quantity": item['quantity'], "options": {}}
                            break
        
        session_attrs['parsedOrder'] = json.dumps({'order_items': order_items}, cls=DecimalEncoder)
        return handle_dialog(event)

    except Exception as e:
        print(f"MODIFICATION: Error during modification: {e}")
        traceback.print_exc()
        message = "I'm sorry, I had trouble understanding that change. Could you try rephrasing?"
        return elicit_slot(event, session_attrs, 'ModificationRequest', message)
        
def handle_dialog(event):
    intent = event['sessionState']['intent']
    slots = intent.get('slots', {})
    session_attrs = event['sessionState'].get('sessionAttributes', {}) or {}
    confirmation_state = intent.get('confirmationState')

    if confirmation_state == 'Confirmed':
        message = "Great, I've got your order down. Before I place it with the kitchen, are there any allergies or dietary restrictions I should know about?"
        response = {
            'sessionState': {
                'dialogAction': {'type': 'ElicitIntent'},
                'sessionAttributes': session_attrs 
            },
            'messages': [{'contentType': 'PlainText', 'content': message}]
        }
        print(f"RESPONSE to Lex: {json.dumps(response)}")
        return response

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
                item['options'][option_name_to_set] = choice_value
                order_items[i] = item; break
        session_attrs['parsedOrder'] = json.dumps({"order_items": order_items}, cls=DecimalEncoder)
        slots['OptionChoice'] = None

    if not slots.get('OrderQuery') and not session_attrs.get('parsedOrder'):
        return elicit_slot(event, session_attrs, 'OrderQuery', "Sure — what would you like to order?")

    if slots.get('OrderQuery') and not session_attrs.get('initialParseComplete'):
        raw_order_text = slots['OrderQuery']['value']['interpretedValue']
        try:
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
                    
                    # Combine options from both parser and keyword detector
                    all_detected_options = {**options, **_check_if_option_in_item_name(parsed_name, menu_entry)}
                    
                    # --- MODIFIED --- Use the new normalization function
                    validated_options = _normalize_options(all_detected_options, menu_entry)
                    
                    normalized_items.append({"item_name": menu_entry['raw_item'].get('ItemName'), "normalized_key": best_key, "quantity": quantity, "options": validated_options, "category": menu_entry.get('category'), "price": menu_entry.get('price'), "item_number": menu_entry.get('item_number')})
                else:
                    normalized_items.append({"item_name": parsed_name, "normalized_key": None, "quantity": quantity, "options": options})
            
            if session_attrs.pop('is_fallback_order', None) and not any(item.get('normalized_key') for item in normalized_items):
                print("MITIGATION: Fallback triggered but no valid menu items found.")
                message = "I'm sorry, I can only take food and drink orders. I didn't recognize any menu items in your request. Could you try again?"
                return elicit_slot(event, {}, 'OrderQuery', message, reset=True)
                
            session_attrs['parsedOrder'] = json.dumps({"order_items": normalized_items}, cls=DecimalEncoder)
            session_attrs['initialParseComplete'] = "true"
        except Exception as e:
            print(f"Error during parsing: {e}"); traceback.print_exc()
            return close_dialog(event, session_attrs, 'Failed', {'contentType': 'PlainText', 'content': "I had trouble understanding that. Could you please try again?"})
    
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
                        is_provided = any(k == opt_meta.get('raw_name') for k in provided_options.keys())
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

    # In your handle_dialog function, replace the existing DrinkQuery block with this one.

# --- MODIFIED BLOCK to correctly parse drinks ---
    if slots.get('DrinkQuery') and slots['DrinkQuery'].get('value'):
        parsed_order = json.loads(session_attrs.get('parsedOrder', '{}'))
        order_items = parsed_order.get('order_items', [])
        drink_text = slots['DrinkQuery']['value']['interpretedValue']

        # Don't do a simple fuzzy find. Use the powerful parser instead.
        try:
            # Re-use the OpenRouter parser for the drink query
            parsed_drinks = invoke_openrouter_parser(drink_text)
            _, menu_lookup, embeddings_cache = get_menu()

            for drink_item in parsed_drinks.get('order_items', []):
                if not isinstance(drink_item, dict): continue
                parsed_name = drink_item.get('item_name', '')
                if not parsed_name: continue
                
                quantity = int(drink_item.get('quantity', 1))
                options = drink_item.get('options') if isinstance(drink_item.get('options'), dict) else {}
                
                # Find the best match for the parsed drink name
                best_key, _ = _fuzzy_find(_normalize_name(parsed_name), menu_lookup, embeddings_cache)
                if best_key:
                    menu_entry = menu_lookup[best_key]
                    
                    # Combine options from parser and keyword detector
                    all_detected_options = {**options, **_check_if_option_in_item_name(parsed_name, menu_entry)}
                    validated_options = _normalize_options(all_detected_options, menu_entry)
                    
                    # Append the fully parsed drink item to the order
                    order_items.append({
                        "item_name": menu_entry['raw_item'].get('ItemName'),
                        "normalized_key": best_key,
                        "quantity": quantity,
                        "options": validated_options,
                        "category": menu_entry.get('category')
                    })
            
            # We've processed the drink, so clear the slot to prevent re-processing
            slots['DrinkQuery'] = None
            
            session_attrs['parsedOrder'] = json.dumps({'order_items': order_items}, cls=DecimalEncoder)
            
        except Exception as e:
            print(f"Error during DRINK parsing: {e}"); traceback.print_exc()
            # If parsing fails, you might want to ask the user to clarify
            return elicit_slot(event, session_attrs, 'DrinkQuery', "I had a little trouble understanding your drink order. Could you say it again?")
    # --- END MODIFIED BLOCK ---
    
    if session_attrs.get('parsedOrder'):
        final_order_items = json.loads(session_attrs['parsedOrder']).get('order_items', [])
        summary_parts = []
        for item in final_order_items:
            options_str = ""
            if item.get('options'): 
                # Use a set to get unique values for the summary string
                unique_options = set(item['options'].values())
                options_str = " (" + ", ".join(unique_options) + ")"
            summary_parts.append(f"{item['quantity']} {item['item_name']}{options_str}")
        summary = "Okay, I have: " + ", ".join(summary_parts) + ". Is that correct?"
        return confirm_intent(event, session_attrs, summary)
    return delegate(event, session_attrs)
    
def fulfill_order(event, allergy_info=None):
    try:
        session_attrs = event['sessionState'].get('sessionAttributes', {})
        final_order_str = session_attrs.get('parsedOrder', '{}')
        final_order = json.loads(final_order_str)
        
        summary_parts = []
        for item in final_order.get('order_items', []):
            options_str = ""
            if item.get('options'): 
                unique_options = set(item['options'].values())
                options_str = " (" + ", ".join(unique_options) + ")"
            summary_parts.append(f"{item['quantity']} {item['item_name']}{options_str}")
        order_summary = ", ".join(summary_parts)
        
        summary = f"Thank you! Your order for {order_summary} has been placed."
        if allergy_info:
            summary += f" We have noted your allergy information: {allergy_info}."
            
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
            if 'order_items' not in parsed_json or not isinstance(parsed_json.get('order_items'), list):
                return {'order_items': []}
            return parsed_json
        return {'order_items': []}
    except Exception as e:
        print(f"Error calling OpenRouter: {e}"); traceback.print_exc()
        return {'order_items': []}
def elicit_slot(event, session_attrs, slot_to_elicit, message_content, reset=False):
    intent = event['sessionState']['intent']
    if reset:
        intent['slots'] = {"OrderQuery": None, "DrinkQuery": None, "OptionChoice": None}
        session_attrs = {}
    response = {'sessionState': {'dialogAction': {'type': 'ElicitSlot', 'slotToElicit': slot_to_elicit}, 'intent': intent, 'sessionAttributes': session_attrs}, 'messages': [{'contentType': 'PlainText', 'content': message_content}]}
    print(f"RESPONSE to Lex: {json.dumps(response)}")
    return response
def confirm_intent(event, session_attrs, message_content):
    response = {'sessionState': {'dialogAction': {'type': 'ConfirmIntent'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}, 'messages': [{'contentType': 'PlainText', 'content': message_content}]}
    print(f"RESPONSE to Lex: {json.dumps(response)}")
    return response
def delegate(event, session_attrs):
    response = {'sessionState': {'dialogAction': {'type': 'Delegate'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}}
    print(f"RESPONSE to Lex: {json.dumps(response)}")
    return response
def close_dialog(event, session_attrs, fulfillment_state, message):
    event['sessionState']['intent']['state'] = fulfillment_state
    response = {'sessionState': {'dialogAction': {'type': 'Close'}, 'intent': event['sessionState']['intent'], 'sessionAttributes': session_attrs}, 'messages': [message]}
    print(f"RESPONSE to Lex: {json.dumps(response)}")
    return response