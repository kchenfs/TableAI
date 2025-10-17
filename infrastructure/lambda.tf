# /infrastructure/lambda.tf

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
        Effect   = "Allow"
        Resource = [
          data.aws_dynamodb_table.menu.arn,
          data.aws_dynamodb_table.orders.arn

        ]
      },
      # Permission to get the ECR authorization token
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      # Permissions to push and pull images from the specific ECR repository
      {
        Sid    = "AllowEcrImagePushPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart"
        ]
        Resource = aws_ecr_repository.momotaro_lex_bot.arn
      } # The extra brace was removed from here
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
  timeout       = 90
  memory_size = 2048

  package_type = "Image"

 image_uri = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/${aws_ecr_repository.momotaro_lex_bot.name}:latest"


  environment {
    variables = {
      MENU_TABLE_NAME   = data.aws_dynamodb_table.menu.name
      ORDERS_TABLE_NAME = data.aws_dynamodb_table.orders.name
      OPENROUTER_API_KEY = var.openrouter_api_key
      GOOGLE_API_KEY      = var.google_api_key
      S3_BUCKET_NAME      = data.aws_s3_bucket.momotaro-assets.bucket
    }
  }

  tags = {
    Name    = "TableAI Lex Fulfillment Lambda"
    Project = "TableAI1"
  }
}

resource "aws_lambda_permission" "lex_invoke" {
  statement_id  = "AllowLexV2ToInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lex_fulfillment_handler.function_name
  principal     = "lexv2.amazonaws.com"

  # IMPORTANT: You must replace the placeholders below with references to your
  # Lex Bot and Bot Alias resources defined elsewhere in your Terraform code.
  source_arn = "arn:aws:lex:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:bot-alias/S832QRVZP3/TSTALIASID"
}