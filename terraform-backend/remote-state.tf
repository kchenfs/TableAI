# /terraform-backend/main.tf

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.15"
    }
  }
}

provider "aws" {
  region = "ca-central-1"
}

# IMPORTANT: S3 bucket names must be globally unique.
# Change "your-unique-identifier" to something unique like your name or a random string.
variable "s3_bucket_name" {
  description = "A unique name for the S3 bucket to store Terraform state."
  default     = "tableai-remote-state"
}

# 1. S3 Bucket to store the terraform.tfstate file
resource "aws_s3_bucket" "tfstate" {
  bucket = var.s3_bucket_name

  # Protect against accidental deletion of the state file
  lifecycle {
    prevent_destroy = true
  }
}

# Enable versioning on the S3 bucket to keep a history of state files
resource "aws_s3_bucket_versioning" "tfstate_versioning" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

# 2. DynamoDB Table for state locking
# This prevents multiple people from running `terraform apply` at the same time.
resource "aws_dynamodb_table" "tflocks" {
  name         = "TableAI-TerraformStateLocks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}