# TableAI: Serverless Generative AI Ordering System

**TableAI** is a fully serverless, event-driven conversational agent designed to streamline restaurant operations. Unlike rigid, rule-based chatbots, TableAI leverages **Large Language Models (LLMs)** and **Vector Embeddings** to perform two critical functions:
1.  **Complex Ordering:** Handling natural language, multi-item orders, and modifications.
2.  **Intelligent Q&A:** Answering customer inquiries (e.g., store hours, location, policies) using semantic search.

Built entirely on **AWS** using **Terraform**, this project demonstrates modern cloud-native architecture patterns including **Hybrid Search** and **Vector-Based Knowledge Retrieval**.

---

## 🏗 System Architecture

This system follows a serverless microservices pattern, utilizing **Amazon Lex** for state management and **AWS Lambda** for orchestration, while offloading cognitive tasks to specialized AI models via API.

```mermaid
graph TD
    %% Styles
    classDef aws fill:#FF9900,stroke:#232F3E,stroke-width:2px,color:white;
    classDef ai fill:#8A2BE2,stroke:#4B0082,stroke-width:2px,color:white;
    classDef ext fill:#eeeeee,stroke:#333,stroke-width:2px;
    classDef user fill:#fff,stroke:#333,stroke-width:2px,stroke-dasharray: 5 5;

    %% Actors
    User((User)):::user

    %% AWS Cloud Environment
    subgraph AWS_Cloud [AWS Cloud Environment]
        direction TB
        Lex["Amazon Lex V2<br/>(ASR & NLU)"]:::aws
        Lambda["AWS Lambda<br/>(Orchestration Logic)"]:::aws
        DDB[("Amazon DynamoDB<br/>Menu + Knowledge Base")]:::aws
        Cognito["Amazon Cognito<br/>(Guest Identity)"]:::aws
    end

    %% External AI Services
    subgraph AI_Services [External AI APIs]
        Llama["OpenRouter API<br/>Llama 3.3 (Router & Parser)"]:::ai
        Gemini["Google Gemini API<br/>Embeddings"]:::ai
    end

    %% Future Integration
    subgraph External_App [External Integration]
        TableTap["TableTap E-Commerce<br/>(Checkout Flow)"]:::ext
    end

    %% Connections
    User --> |"Voice or Text"| Lex
    Lex -.-> |"Auth Check"| Cognito
    Lex --> |"JSON Event"| Lambda
    
    %% AI Logic Loop
    Lambda --> |"1. Raw Text"| Llama
    Llama --> |"2. Classification & Parsing"| Lambda
    
    Lambda --> |"3. Text to Vector"| Gemini
    Gemini --> |"4. Vector Embedding"| Lambda
    
    %% Database Check
    Lambda <--> |"5. Semantic Search"| DDB
    
    %% Response
    Lambda --> |"6. Fulfillment Response"| Lex
    Lex --> |"Audio/Text Output"| User

    %% Future Path
    Lambda -.-> |"Future: POST /checkout"| TableTap

    %% Link Styling
    linkStyle 10 stroke-width:2px,fill:none,stroke:#FF9900,stroke-dasharray: 5 5;
```
🧠 The AI Pipeline: How it Works
TableAI utilizes a "Router-Retriever-Generator" pattern. When a user speaks, the backend logic dynamically switches between Order Fulfillment and Knowledge Retrieval.

    
