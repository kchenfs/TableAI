# /infrastructure/lex.tf

locals {
  sample_utterances_json = jsonencode([
    { utterance = "I want to order food" },
    { utterance = "I would like to place an order" },
    { utterance = "Can I get a {OrderQuery}" },
    { utterance = "I want a {OrderQuery} and a {DrinkQuery}" },
    { utterance = "I'd like to order a {OrderQuery}" },
    { utterance = "{OrderQuery} and {DrinkQuery} please" }
  ])

  fulfillment_hook_json = jsonencode({
    enabled = true
  })
  
  # NEW: Configuration for the dialog code hook
  dialog_hook_json = jsonencode({
    enabled = true
  })

  # UPDATED: Slot priorities including the new slots
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

# These resources write the JSON content to temporary files
resource "local_file" "sample_utterances" {
  content  = local.sample_utterances_json
  filename = "${path.module}/tmp/sample_utterances.json"
}

resource "local_file" "fulfillment_hook" {
  content  = local.fulfillment_hook_json
  filename = "${path.module}/tmp/fulfillment_hook.json"
}

# NEW: Local file for the dialog hook JSON
resource "local_file" "dialog_hook" {
  content  = local.dialog_hook_json
  filename = "${path.module}/tmp/dialog_hook.json"
}

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
  [cite_start]type                        = "Bot" [cite: 1]
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
# 3. Define the intent (minimal)
######################################
resource "aws_lexv2models_intent" "order_food" {
  bot_id      = aws_lexv2models_bot.tableai_bot.id
  bot_version = aws_lexv2models_bot_locale.en_us.bot_version
  locale_id   = aws_lexv2models_bot_locale.en_us.locale_id
  name        = "OrderFood"
  # Note: Details like utterances and hooks are now patched later
  # to manage dependencies.
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
  [cite_start]name         = "OrderQuery" [cite: 1]
  [cite_start]slot_type_id = "AMAZON.FreeFormInput" [cite: 1]
  description  = "Captures the user's entire free-form food order."
  value_elicitation_setting {
    [cite_start]slot_constraint = "Required" [cite: 1]
    prompt_specification {
      max_retries                = 2
      allow_interrupt            = true
      message_group {
        message {
          plain_text_message {
            [cite_start]value = "Certainly, what would you like to order?" [cite: 1]
          }
        }
      }
    }
  }
}

# NEW: Slot to capture the drink order
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
      max_retries = 2
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
}

# NEW: Slot to confirm the final order
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
      max_retries = 2
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
}

######################################
# 6. Patch intent after slot creation (File-Based)
######################################
resource "null_resource" "update_intent_with_all_details" {
  depends_on = [
    local_file.sample_utterances,
    local_file.fulfillment_hook,
    local_file.dialog_hook,
    local_file.slot_priorities
  ]

  provisioner "local-exec" {
    interpreter = ["powershell", "-Command"]
    # UPDATED: This command now includes the dialog_code_hook
    command = "aws lexv2-models update-intent --bot-id ${aws_lexv2models_bot.tableai_bot.id} --bot-version DRAFT --locale-id ${aws_lexv2models_bot_locale.en_us.locale_id} --intent-id ${aws_lexv2models_intent.order_food.intent_id} --intent-name ${aws_lexv2models_intent.order_food.name} --sample-utterances file://${local_file.sample_utterances.filename} --fulfillment-code-hook file://${local_file.fulfillment_hook.filename} --dialog-code-hook file://${local_file.dialog_hook.filename} --slot-priorities file://${local_file.slot_priorities.filename}"
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
  source_arn    = awscc_lex_bot_alias.prod.arn
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