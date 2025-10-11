# Reference your EXISTING DynamoDB tables
data "aws_dynamodb_table" "menu" {
  name = "Momotaro-Dashboard-Menu" # <-- Make sure this is your exact table name
}

data "aws_dynamodb_table" "orders" {
  name = "momotaroOrdersDatabase" # <-- Make sure this is your exact table name
}

data "aws_iam_policy_document" "cognito_unauth_policy_doc" {
  statement {
    effect = "Allow"
    actions = [
      "lex:RecognizeText",
      "lex:RecognizeUtterance",
      "polly:SynthesizeSpeech" # Add this permission
    ]
    resources = [
      "arn:aws:lex:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:bot-alias/${aws_lexv2models_bot.tableai_bot.id}/${awscc_lex_bot_alias.prod.bot_alias_id}",
      "*" # Polly SynthesizeSpeech does not support resource-level permissions
    ]
  }
}