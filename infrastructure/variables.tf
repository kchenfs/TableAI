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