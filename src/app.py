def handle_order_food(event):
    """
    Handles the logic for the OrderFood intent.
    - Extracts the raw order query from the slot.
    - Prepares a prompt for Bedrock.
    - (Future step) Sends the prompt to Bedrock to parse the order.
    - Returns a confirmation to the user.
    """
    # 1. Extract the single 'OrderQuery' slot from the Lex event
    slots = event['sessionState']['intent']['slots']
    raw_order_text = slots.get('OrderQuery', {}).get('value', {}).get('interpretedValue')

    # If for some reason the slot is empty, ask the user for their order.
    if not raw_order_text:
        # This part of the response tells Lex to ask the user for the slot again.
        return {
            'sessionState': {
                'dialogAction': {
                    'type': 'ElicitSlot',
                    'slotToElicit': 'OrderQuery',
                },
                'intent': event['sessionState']['intent']
            }
        }

    # 2. (Future Step) This is where you will add your Bedrock logic.
    #    - Fetch your menu from DynamoDB.
    #    - Create the detailed prompt with the menu and the 'raw_order_text'.
    #    - Call the Bedrock client.
    #    - Parse the JSON response from Bedrock.
    #    - Save the structured order to the 'orders_table'.
    print(f"Received raw order query: '{raw_order_text}'. Next step is to process with Bedrock.")

    # 3. For now, return a simple confirmation to Lex to close the conversation.
    #    Later, you'll make this confirmation more dynamic based on Bedrock's response.
    message = f"Thank you! I'm processing your order: '{raw_order_text}'."
    return close_dialog(event, 'Fulfilled', {'contentType': 'PlainText', 'content': message})