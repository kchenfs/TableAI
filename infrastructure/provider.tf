terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.15.0"
    }

    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
    # Add this new provider
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.58.0" # Use a recent version
    }
  }
}
provider "aws" {
  region = "ca-central-1"
}
# Also configure the awscc provider
provider "awscc" {
  region = "ca-central-1"
}