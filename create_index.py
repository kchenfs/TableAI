import json
import google.generativeai as genai
import numpy as np
import faiss
import os

# --- CONFIGURATION ---
# IMPORTANT: Set your Google API Key as an environment variable before running.
# On Mac/Linux: export GOOGLE_API_KEY="YOUR_API_KEY"
# On Windows: set GOOGLE_API_KEY="YOUR_API_KEY"
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY environment variable not set.")

genai.configure(api_key=GOOGLE_API_KEY)
GEMINI_EMBEDDING_MODEL = 'models/embedding-001'
KNOWLEDGE_BASE_FILE = 'knowledge_base.json'
OUTPUT_INDEX_FILE = 'rag_index.faiss'
OUTPUT_CHUNKS_FILE = 'rag_chunks.json'

def create_and_save_index():
    """
    Reads the knowledge base, creates embeddings, builds a FAISS index,
    and saves the index and chunks to local files.
    """
    print(f"Loading knowledge base from {KNOWLEDGE_BASE_FILE}...")
    try:
        with open(KNOWLEDGE_BASE_FILE, 'r') as f:
            kb = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: The file '{KNOWLEDGE_BASE_FILE}' was not found.")
        print("Please create it in the same directory as this script.")
        return

    # 1. Chunking the knowledge base
    chunks = []
    chunks.append(f"Restaurant Info: {json.dumps(kb['restaurantInfo'])}")
    for item in kb['menuItems']:
        chunks.append(f"Menu Item: {json.dumps(item)}")
    
    print(f"Created {len(chunks)} text chunks.")

    # 2. Embedding the chunks in a batch
    print("Generating embeddings with Gemini...")
    try:
        response = genai.embed_content(
            model=GEMINI_EMBEDDING_MODEL,
            content=chunks,
            task_type="RETRIEVAL_DOCUMENT"
        )
        embeddings = response['embedding']
        print(f"Successfully generated {len(embeddings)} embeddings.")
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return

    # 3. Creating and storing the FAISS index
    print("Building FAISS index...")
    embedding_dim = len(embeddings[0])
    index = faiss.IndexFlatL2(embedding_dim)
    index.add(np.array(embeddings))
    print(f"FAISS index built successfully. Total vectors: {index.ntotal}")

    # 4. Saving the files
    print(f"Saving FAISS index to {OUTPUT_INDEX_FILE}...")
    faiss.write_index(index, OUTPUT_INDEX_FILE)

    print(f"Saving text chunks to {OUTPUT_CHUNKS_FILE}...")
    with open(OUTPUT_CHUNKS_FILE, 'w') as f:
        json.dump(chunks, f)

    print("\nProcess complete!")
    print(f"Upload '{OUTPUT_INDEX_FILE}' and '{OUTPUT_CHUNKS_FILE}' to your S3 bucket.")

if __name__ == '__main__':
    create_and_save_index()

