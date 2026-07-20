# Order-status endpoint: a zip Lambda + public Function URL the chat page polls.
data "archive_file" "status_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../status_lambda"
  output_path = "${path.module}/status_lambda.zip"
  excludes    = ["test_status.py"]
}

resource "aws_iam_role" "order_status_role" {
  name = "OrderStatusRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow",
      Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "order_status_policy" {
  name = "OrderStatusPolicy"
  role = aws_iam_role.order_status_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" },
      { Effect = "Allow", Action = "dynamodb:GetItem", Resource = data.aws_dynamodb_table.orders.arn },
    ]
  })
}

resource "aws_lambda_function" "order_status" {
  function_name    = "OrderStatus"
  role             = aws_iam_role.order_status_role.arn
  runtime          = "python3.13"
  handler          = "status_app.lambda_handler"
  filename         = data.archive_file.status_zip.output_path
  source_code_hash = data.archive_file.status_zip.output_base64sha256
  timeout          = 10
  environment { variables = { ORDERS_TABLE_NAME = data.aws_dynamodb_table.orders.name } }
}

resource "aws_lambda_function_url" "order_status_url" {
  function_name      = aws_lambda_function.order_status.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    allow_methods = ["GET"]
  }
}

output "order_status_url" {
  value = aws_lambda_function_url.order_status_url.function_url
}
