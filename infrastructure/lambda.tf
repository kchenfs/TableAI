# terraform/lambda.tf

# Data sources to get current region for ARNs and context
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ------------------------------------------------------------------------------
# IAM ROLE & POLICY FOR THE LexFulfillment LAMBDA
# This role grants the Lambda function the necessary permissions to execute and
# interact with other AWS services like DynamoDB, CloudWatch Logs, and Bedrock.
# ------------------------------------------------------------------------------
resource "aws_iam_role" "lex_fulfillment_role" {
  name = "TableAILexFulfillmentRole"

  # Standard trust policy that allows the Lambda service to assume this role.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      },
    ]
  })

  tags = {
    Project = "TableAI"
  }
}

resource "aws_iam_policy" "lex_fulfillment_policy" {
  name        = "TableAILexFulfillmentPolicy"
  description = "Policy for the TableAI Lex Fulfillment Lambda function"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Standard permissions for Lambda to write logs to CloudWatch.
      {
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Effect   = "Allow"
        Resource = "arn:aws:logs:*:*:*"
      },
      # Permissions to read from the Menu table and write to the Orders table.
      {
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Effect = "Allow"
        Resource = [
          data.aws_dynamodb_table.menu.arn,
          data.aws_dynamodb_table.orders.arn
        ]
      },
      # IMPORTANT: Bedrock models are not yet in ca-central-1.
      # We explicitly grant permission to the us-east-1 endpoint for model invocation.
      # Your Lambda code will need to specify "us-east-1" when creating the Bedrock client.
      {
        Action   = "bedrock:InvokeModel"
        Effect   = "Allow"
        Resource = "arn:aws:bedrock:ca-central-1::foundation-model/*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lex_fulfillment_attach" {
  role       = aws_iam_role.lex_fulfillment_role.name
  policy_arn = aws_iam_policy.lex_fulfillment_policy.arn
}

# ------------------------------------------------------------------------------
# LexFulfillment LAMBDA FUNCTION
# This is the core business logic of the application. It receives data from Lex,
# queries DynamoDB for menu items, calls Bedrock for AI recommendations, and
# saves the final order back to DynamoDB.
# ------------------------------------------------------------------------------
resource "aws_lambda_function" "lex_fulfillment_handler" {
  function_name = "TableAILexFulfillmentHandler"
  role          = aws_iam_role.lex_fulfillment_role.arn
  handler       = "app.lambda_handler" # File is app.py, function is lambda_handler
  runtime       = "python3.13"
  timeout       = 30 # Increased timeout for potential cold starts and AI model calls

  # Assumes your code is in a /src folder at the root of your project
  filename         = "../src/lambda_fulfillment.zip"
  source_code_hash = filebase64sha256("../src/lambda_fulfillment.zip")

  environment {
    variables = {
      MENU_TABLE_NAME   = data.aws_dynamodb_table.menu.name
      ORDERS_TABLE_NAME = data.aws_dynamodb_table.orders.name
      BEDROCK_REGION    = "ca-central-1" # Pass the Bedrock region to the code
    }
  }

  tags = {
    Name    = "TableAI Lex Fulfillment Lambda"
    Project = "TableAI"
  }
}