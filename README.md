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

  Step 2️⃣: For each item_name, call Embedding Microservice
     ↓
  [Embedding Microservice on EC2/ECS]
     ↳ Task: Return embedding vector for "dragon roll"
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
