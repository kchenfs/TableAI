# /infrastructure/lex.tf

# 1. The Lex V2 Bot itself
resource "aws_lexv2models_bot" "tableai_bot" {
  name     = "TableAIOrderBot"
  role_arn = aws_iam_role.lex_fulfillment_role.arn
  data_privacy {
    child_directed = false
  }
  idle_session_ttl_in_seconds = 300
}

# 2. Define the language/locale for the bot
resource "aws_lexv2models_bot_locale" "en_us" {
  bot_id                           = aws_lexv2models_bot.tableai_bot.id
  bot_version                      = "DRAFT"
  locale_id                        = "en_US"
  n_lu_intent_confidence_threshold = 0.40
}

# 3. Define a basic intent
resource "aws_lexv2models_intent" "order_food" {
  bot_id      = aws_lexv2models_bot.tableai_bot.id
  bot_version = aws_lexv2models_bot_locale.en_us.bot_version
  locale_id   = aws_lexv2models_bot_locale.en_us.locale_id
  name        = "OrderFood"

  sample_utterance {
    utterance = "I want to order food"
  }
  sample_utterance {
    utterance = "I would like to place an order"
  }

  fulfillment_code_hook {
    enabled = true
  }
}

# 3a. Add a small delay to allow intent to be fully ready
resource "time_sleep" "wait_for_intent" {
  depends_on = [aws_lexv2models_intent.order_food]
  create_duration = "10s"
}

# 3b. Create the slot BEFORE creating the bot version
resource "aws_lexv2models_slot" "order_query_slot" {
  bot_id      = aws_lexv2models_bot.tableai_bot.id
  bot_version = "DRAFT"
  intent_id   = aws_lexv2models_intent.order_food.intent_id
  locale_id   = aws_lexv2models_bot_locale.en_us.locale_id

  name         = "OrderQuery"
  slot_type_id = "AMAZON.SearchQuery"
  description  = "Captures the user's entire free-form order."

  value_elicitation_setting {
    slot_constraint = "Required"

    prompt_specification {
      max_retries                = 2
      allow_interrupt            = true
      message_selection_strategy = "Random"

      message_group {
        message {
          plain_text_message {
            value = "Certainly, what would you like to order?"
          }
        }
      }
      message_group {
        message {
          plain_text_message {
            value = "I can help with that. What are we getting for you today?"
          }
        }
      }
    }
  }

  depends_on = [
    aws_lexv2models_intent.order_food,
    aws_lexv2models_bot_locale.en_us
  ]
}

# 4. Create a version of the bot from the DRAFT state
# NOW depends on the slot being created first
resource "aws_lexv2models_bot_version" "v1" {
  bot_id = aws_lexv2models_bot.tableai_bot.id
  locale_specification = {
    (aws_lexv2models_bot_locale.en_us.locale_id) = {
      source_bot_version = aws_lexv2models_bot_locale.en_us.bot_version
    }
  }
  depends_on = [
    aws_lexv2models_intent.order_food,
    aws_lexv2models_slot.order_query_slot
  ]
}

# 5. Create an alias using the AWSCC provider
resource "awscc_lex_bot_alias" "prod" {
  bot_alias_name = "prod"
  bot_id         = aws_lexv2models_bot.tableai_bot.id
  bot_version    = aws_lexv2models_bot_version.v1.bot_version

  bot_alias_locale_settings = [{
    locale_id = aws_lexv2models_bot_locale.en_us.locale_id
    bot_alias_locale_setting = {
      enabled = true
      code_hook_specification = {
        lambda_code_hook = {
          lambda_arn                  = aws_lambda_function.lex_fulfillment_handler.arn
          code_hook_interface_version = "1.0"
        }
      }
    }
  }]
}

# 6. Grant Lex permission to invoke the Lambda function
resource "aws_lambda_permission" "allow_lex" {
  statement_id  = "AllowLexV2ToInvokeLambda"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lex_fulfillment_handler.function_name
  principal     = "lexv2.amazonaws.com"
  source_arn    = awscc_lex_bot_alias.prod.arn
}