import boto3
from decimal import Decimal
import google.generativeai as genai
import time

# --- Configuration ---
MENU_TABLE_NAME = 'MomotaroSushiMenu_DB'   # Your DynamoDB table
MODEL_NAME = 'models/embedding-001'        # Gemini embedding model
API_KEY = 'U'            # Replace with your Gemini API key
BATCH_SIZE = 10                            # Adjust for speed vs API limit

# --- Setup Clients ---
dynamodb = boto3.resource('dynamodb')
menu_table = dynamodb.Table(MENU_TABLE_NAME)
genai.configure(api_key=API_KEY)

# --- Fetch Menu Items ---
print("Fetching all menu items from DynamoDB...")
response = menu_table.scan()
items = response.get('Items', [])
print(f"Found {len(items)} items. Generating and saving embeddings...")

# --- Helper Function ---
def get_embedding(text):
    """Calls Gemini Embedding API and returns a list of floats"""
    try:
        result = genai.embed_content(
            model=MODEL_NAME,
            content=text,
            task_type="RETRIEVAL_DOCUMENT"
        )
        return result['embedding']
    except Exception as e:
        print(f"Error embedding text: {text[:50]}... → {e}")
        return None

# --- Generate and Save Embeddings ---
with menu_table.batch_writer() as batch:
    for i, item in enumerate(items, 1):
        item_name = item.get('ItemName', '')
        description = item.get('Description', '')
        text_to_embed = f"{item_name} - {description}".strip()

        if not text_to_embed:
            print(f"Skipping empty item {i}")
            continue

        embedding = get_embedding(text_to_embed)
        if not embedding:
            continue

        # Convert floats to Decimal (DynamoDB doesn’t support float directly)
        embedding_decimals = [Decimal(str(f)) for f in embedding]

        # Update item
        item['ItemEmbedding'] = embedding_decimals
        batch.put_item(Item=item)

        print(f"[{i}/{len(items)}] Embedded: {item_name}")

        # Rate limiting to stay under 100 RPM
        if i % BATCH_SIZE == 0:
            time.sleep(1)

print("✅ Embedding generation and update complete!")
