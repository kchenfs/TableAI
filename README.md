# TableTap: AI-Powered Voice & Text Ordering System

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
