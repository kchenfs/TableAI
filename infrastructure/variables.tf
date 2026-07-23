# /infrastructure/variables.tf

variable "openrouter_api_key" {
  type        = string
  description = "The API key for the OpenRouter service."
  sensitive   = true
}
variable "google_api_key" {
  type        = string
  description = "The API key for Google services."
  sensitive   = true
}

variable "order_events_topic_arn" {
  description = "SNS order_events topic from the Fanout stack"
  type        = string
  default     = "arn:aws:sns:ca-central-1:798965869505:MomotaroFanoutStack-MomotaroOrderEventsTopic94459F09-vMCrg59Bej1p"
}

variable "stripe_secret_param" {
  description = "SSM SecureString name for the Stripe secret key"
  type        = string
  default     = "/momotaro/prod/STRIPE_SECRET_KEY"
}

variable "openrouter_model" {
  description = "OpenRouter model slug for the fulfillment Lambda's LLM calls. Use a NON-reasoning instruct model — reasoning models (e.g. gpt-oss) emit reasoning tokens and return empty content under tight max_tokens caps."
  type        = string
  default     = "google/gemma-4-31b-it:free"
}