# TableAI: AI-Powered Voice & Text Ordering System

This project is a fully serverless, AI-enhanced ordering system built on AWS. It allows users to order from a menu using natural language via text or voice, without needing to sign in.

## Architecture Diagram

!
*(Include the Mermaid diagram image here)*

## Core Features

* **Zero-Friction Ordering:** No login required, thanks to temporary guest credentials from Amazon Cognito.
* **Natural Language Understanding:** Uses Amazon Lex for conversation flow and Amazon Bedrock (with Claude) for advanced NLU and menu recommendations.
* **Serverless & Scalable:** Built entirely on serverless AWS services like Lambda, DynamoDB, and Lex.
* **Infrastructure as Code (IaC):** All infrastructure is defined and managed using Terraform for automated, repeatable deployments.
* **Automated Deployments:** A CI/CD pipeline using GitHub Actions automatically deploys infrastructure and code changes.

## Tech Stack

* **Conversational AI:** Amazon Lex V2, Amazon Bedrock
* **Compute:** AWS Lambda (Python)
* **Database:** Amazon DynamoDB
* **Identity:** Amazon Cognito Identity Pools
* **Infrastructure & Automation:** Terraform, GitHub Actions
* **Frontend:** HTML/JS on AWS S3 & CloudFront

## Getting Started

### Prerequisites
* AWS Account
* Terraform installed
* AWS CLI configured

### Deployment
1. Clone the repository: `git clone ...`
2. Navigate to the `infrastructure` directory: `cd infrastructure`
3. Initialize Terraform: `terraform init`
4. Deploy the stack: `terraform apply`

*(Add more detailed steps as you build)*



[Amazon Lex]  
  ↓
[AWS Lambda] — receives user message → "2 dragon rolls and a Nestea"

  Step 1️⃣: Invoke LLM
     ↓
  [LLM API via OpenRouter or Bedrock or OpenAI]
     ↳ Task: Parse free-form text → structured JSON
         → {"order_items": [{"item_name": "dragon roll", "quantity": 2},
                            {"item_name": "nestea", "quantity": 1}]}

  Step 2️⃣: For each item_name, call Embedding Service Gemini Model
     ↓
   Return embedding vector for "dragon roll"
     ↓
  [Lambda] compares to precomputed DynamoDB embeddings
     ↳ Finds closest match (e.g., "Green Dragon Roll")

  Step 3️⃣: Validate and update Lex slots or session attributes
     ↓
  [Lambda + Lex Dialogue Management]
     ↳ If item has required Options → ask user ("Would you like beef or vegetable?")
     ↳ Else → confirm item, proceed to checkout

| Model                                        | Task                            | Input                 | Output                 | Where it Runs                     |
| -------------------------------------------- | ------------------------------- | --------------------- | ---------------------- | --------------------------------- |
| **LLM (e.g. Llama, Gemini, GPT)**            | Instruction following (parsing) | Raw customer text     | JSON (item + qty)      | External API (Bedrock/OpenRouter) |
| **Embedding model (e.g. MiniLM, BGE-Small)** | Semantic search                 | Parsed item name      | Closest menu item      | Your microservice on EC2/ECS      |
| **Lex**                                      | Dialogue management             | Structured info       | Prompts / slot filling | AWS-managed                       |
| **Lambda**                                   | Orchestration                   | Lex event + API calls | Response to Lex        | AWS Lambda                        |

[Lex]
 ↓
[AWS Lambda]
   1️⃣ → Call LLM (OpenRouter / Bedrock)
         → {"order_items":[{"item_name":"dragon roll","quantity":2}]}
   2️⃣ → For each item_name → Call Embedding Microservice
         → Match to DynamoDB menu embeddings
   3️⃣ → If item has required options → Lex prompt
         Else → add to session order
   4️⃣ → Return Lex response (ElicitSlot / ConfirmIntent)


   1. Become an Informational Menu Expert (using RAG)
You are already brainstorming this, and it's the most powerful next step. A great assistant doesn't just take orders; it answers questions. This builds trust and helps users make decisions.

What it is: Using the Retrieval-Augmented Generation (RAG) architecture we discussed, you can empower the bot to answer specific questions about your restaurant and menu.

Example Phrases:

"What's in the Green Dragon Roll?"

"Do you have any gluten-free options?"

"How spicy is the Dynamite Roll?"

"What are your hours on Sunday?"

How to Build It:

Create a knowledge base file (menu_details.json or similar) with ingredients, allergen information, spice levels, and restaurant info (hours, address).

In your Lambda's FallbackIntent handler, implement the RAG pipeline: classify the user's intent as a "QUESTION," retrieve relevant info from your knowledge base, and use the Llama model to generate a helpful answer.

2. Handle Order Modifications Gracefully
People frequently change their minds. A bot that can handle this feels much more human and less rigid.

What it is: Allow users to add, remove, or change items after the initial order has been parsed but before it's confirmed.

Example Phrases:

"Actually, can you remove the fried rice?"

"Change the diet coke to a regular coke."

"I also want to add an order of gyoza."

How to Build It:

Create a new intent in Lex called ModifyOrderIntent.

In your Lambda, when this intent is triggered, it would load the current parsedOrder from the session attributes.

You would use your Llama model to interpret the user's request (is it an add, remove, or update action?).

Your code would then modify the order object in the session and present the new, updated order for confirmation. For example: "Okay, I've removed the fried rice. Your order is now 1 Green Dragon Roll. Is that correct?"

3. Offer Recommendations and Suggestions
A great server can help you discover new things. Your bot can do the same.

What it is: Provide recommendations based on popularity or pairings.

Example Phrases:

"What's popular?"

"What do you recommend?"

"What goes well with the sashimi?"

How to Build It:

Simple Approach: Hardcode a list of popular items in your Lambda. When asked, the bot can randomly pick one and suggest it.

Advanced (LLM) Approach: Create an AskRecommendationIntent. In your Lambda, send a prompt to Llama that includes the menu and the user's question. For example: "Given this menu, what would be a good suggestion for a customer asking 'what's popular'?"

4. Remember Past Orders for Quick Reordering
Regular customers love being remembered. This feature provides a massive convenience boost and encourages repeat business.

What it is: For returning users, the bot proactively offers to repeat their last order.

Example Phrases:

(Bot initiates): "Welcome back! Last time you ordered a Green Dragon Roll and a Diet Coke. Would you like to order that again?"

How to Build It:

This requires a way to identify users, even if it's just by sessionId for a short-term memory.

When an order is fulfilled, store the final order details in a DynamoDB table, keyed by the user's identifier.

At the beginning of a new conversation (e.g., in the GreetingIntent or the first OrderFood intent), your Lambda would first check the DynamoDB table for a past order.

If an order is found, the bot can offer the reorder option.
