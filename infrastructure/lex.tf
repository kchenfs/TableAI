# /infrastructure/lex.tf

locals {
  # This JSON is now only for slot priorities, as other settings are defined directly.
  slot_priorities_json = jsonencode([
    {
      priority = 1
      slotId   = aws_lexv2models_slot.order_query_slot.slot_id
    },
    {
      priority = 2
      slotId   = aws_lexv2models_slot.drink_query_slot.slot_id
    },
    {
      priority = 3
      slotId   = aws_lexv2models_slot.confirmation_slot.slot_id
    }
  ])
}

# This resource writes the slot priority JSON to a temporary file for the patch command.
resource "local_file" "slot_priorities" {
  content  = local.slot_priorities_json
  filename = "${path.module}/tmp/slot_priorities.json"
}

######################################
# 1. The Lex V2 Bot
######################################
resource "aws_lexv2models_bot" "tableai_bot" {
  name     = "TableAIOrderBot"
  role_arn = aws_iam_role.lex_fulfillment_role.arn

  data_privacy {
    child_directed = false
  }

  idle_session_ttl_in_seconds = 300
}

######################################
# 2. Define the language/locale
######################################
resource "aws_lexv2models_bot_locale" "en_us" {
  bot_id                           = aws_lexv2models_bot.tableai_bot.id
  bot_version                      = "DRAFT"
  locale_id                        = "en_US"
  n_lu_intent_confidence_threshold = 0.70
}

######################################
# 3. Define the intent (complete version)
######################################
resource "aws_lexv2models_intent" "order_food" {
  bot_id      = aws_lexv2models_bot.tableai_bot.id
  bot_version = aws_lexv2models_bot_locale.en_us.bot_version
  locale_id   = aws_lexv2models_bot_locale.en_us.locale_id
  name        = "OrderFood"

  # Define hooks and utterances directly in the resource for reliability.
  dialog_code_hook {
    enabled = true
  }

  fulfillment_code_hook {
    enabled = true
  }

  sample_utterance {
    utterance = "I want to order food"
  }
  sample_utterance {
    utterance = "I would like to place an order"
  }
  sample_utterance {
    utterance = "I'd like to order"
  }
  sample_utterance {
    utterance = "Can I get some food"
  }
  sample_utterance {
    utterance = "Place an order for me"
  }
}

######################################
# 4. Define the slot type (optional)
######################################
resource "aws_lexv2models_slot_type" "order_query_slot_type" {
  bot_id      = aws_lexv2models_bot.tableai_bot.id
  bot_version = aws_lexv2models_bot_locale.en_us.bot_version
  name        = "OrderQueryType"
  locale_id   = aws_lexv2models_bot_locale.en_us.locale_id

  slot_type_values {
    sample_value {
      value = "Pizza"
    }
  }

  value_selection_setting {
    resolution_strategy = "OriginalValue"
  }
}

######################################
# 5. Define the slots
######################################
resource "aws_lexv2models_slot" "order_query_slot" {
  bot_id       = aws_lexv2models_bot.tableai_bot.id
  bot_version  = "DRAFT"
  intent_id    = aws_lexv2models_intent.order_food.intent_id
  locale_id    = aws_lexv2models_bot_locale.en_us.locale_id
  name         = "OrderQuery"
  slot_type_id = "AMAZON.FreeFormInput"
  description  = "Captures the user's entire free-form food order."
  value_elicitation_setting {
    slot_constraint = "Required"
    prompt_specification {
      max_retries     = 2
      allow_interrupt = true
      message_group {
        message {
          plain_text_message {
            value = "Certainly, what would you like to order?"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      value_elicitation_setting
    ]
  }
}

resource "aws_lexv2models_slot" "drink_query_slot" {
  bot_id       = aws_lexv2models_bot.tableai_bot.id
  bot_version  = "DRAFT"
  intent_id    = aws_lexv2models_intent.order_food.intent_id
  locale_id    = aws_lexv2models_bot_locale.en_us.locale_id
  name         = "DrinkQuery"
  slot_type_id = "AMAZON.FreeFormInput"
  description  = "Captures the user's drink order or a negative response."

  value_elicitation_setting {
    slot_constraint = "Required"
    prompt_specification {
      max_retries     = 2
      allow_interrupt = true
      message_group {
        message {
          plain_text_message {
            value = "Would you like anything to drink?"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      value_elicitation_setting
    ]
  }
}

resource "aws_lexv2models_slot" "confirmation_slot" {
  bot_id       = aws_lexv2models_bot.tableai_bot.id
  bot_version  = "DRAFT"
  intent_id    = aws_lexv2models_intent.order_food.intent_id
  locale_id    = aws_lexv2models_bot_locale.en_us.locale_id
  name         = "Confirmation"
  slot_type_id = "AMAZON.Confirmation"
  description  = "Confirms if the final order is correct."

  value_elicitation_setting {
    slot_constraint = "Required"
    prompt_specification {
      max_retries     = 2
      allow_interrupt = true
      message_group {
        message {
          plain_text_message {
            value = "Is that correct?"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [
      value_elicitation_setting
    ]
  }
}

######################################
# 6. Patch intent with slot priorities
######################################
resource "null_resource" "update_intent_with_all_details" {
  depends_on = [
    aws_lexv2models_slot.order_query_slot,
    aws_lexv2models_slot.drink_query_slot,
    aws_lexv2models_slot.confirmation_slot
  ]

  provisioner "local-exec" {
    interpreter = ["powershell", "-Command"]
    # This command now ONLY updates the slot priorities, which cannot be set on creation.
    command = "aws lexv2-models update-intent --bot-id ${aws_lexv2models_bot.tableai_bot.id} --bot-version DRAFT --locale-id ${aws_lexv2models_bot_locale.en_us.locale_id} --intent-id ${aws_lexv2models_intent.order_food.intent_id} --intent-name ${aws_lexv2models_intent.order_food.name} --slot-priorities file://${local_file.slot_priorities.filename}"
  }
}

######################################
# 7. Create a bot version (builds after intent patch)
######################################
resource "aws_lexv2models_bot_version" "v1" {
  bot_id = aws_lexv2models_bot.tableai_bot.id

  locale_specification = {
    "en_US" = {
      source_bot_version = "DRAFT"
    }
  }

  depends_on = [
    null_resource.update_intent_with_all_details
  ]
}

######################################
# 8. Create alias via AWSCC provider
######################################
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

######################################
# 9. Grant Lex permission to invoke Lambda
######################################
resource "aws_lambda_permission" "allow_lex" {
  statement_id  = "AllowLexV2ToInvokeLambda"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lex_fulfillment_handler.function_name
  principal     = "lexv2.amazonaws.com"
  source_arn    = "arn:aws:lex:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:bot-alias/${aws_lexv2models_bot.tableai_bot.id}/*"
}

######################################
# 10. Outputs
######################################
output "bot_id" {
  value = aws_lexv2models_bot.tableai_bot.id
}

output "intent_id" {
  value = aws_lexv2models_intent.order_food.intent_id
}

output "order_query_slot_id" {
  value = aws_lexv2models_slot.order_query_slot.slot_id
}

output "drink_query_slot_id" {
  value = aws_lexv2models_slot.drink_query_slot.slot_id
}

output "confirmation_slot_id" {
  value = aws_lexv2models_slot.confirmation_slot.slot_id
}