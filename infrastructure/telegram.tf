# Telegram fallback notifier: subscribes to the (CDK-owned) order_events SNS
# topic and DMs paid takeout orders to a Telegram chat — a printer-independent
# backup. Only takeout orders are delivered (filter policy), and TakeoutIngress
# only publishes on payment success, so this fires exactly on PAID takeout.

data "archive_file" "telegram_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../telegram_lambda"
  output_path = "${path.module}/telegram_lambda.zip"
}

variable "telegram_chat_id" {
  description = "Telegram chat id that receives the takeout order alerts"
  type        = string
  default     = ""
}

resource "aws_iam_role" "telegram_notifier_role" {
  name = "TelegramNotifierRole"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "telegram_notifier_policy" {
  name = "TelegramNotifierPolicy"
  role = aws_iam_role.telegram_notifier_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" },
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/momotaro/prod/TELEGRAM_BOT_TOKEN"
      },
      {
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_lambda_function" "telegram_notifier" {
  function_name    = "MomotaroTelegramNotifier"
  role             = aws_iam_role.telegram_notifier_role.arn
  runtime          = "python3.13"
  handler          = "telegram_notifier.handler"
  filename         = data.archive_file.telegram_zip.output_path
  source_code_hash = data.archive_file.telegram_zip.output_base64sha256
  timeout          = 15
  environment {
    variables = { TELEGRAM_CHAT_ID = var.telegram_chat_id }
  }
}

resource "aws_sns_topic_subscription" "telegram_takeout" {
  topic_arn     = var.order_events_topic_arn
  protocol      = "lambda"
  endpoint      = aws_lambda_function.telegram_notifier.arn
  filter_policy = jsonencode({ orderType = ["takeout"] })
}

resource "aws_lambda_permission" "telegram_sns" {
  statement_id  = "AllowSNSInvokeTelegram"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.telegram_notifier.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = var.order_events_topic_arn
}
