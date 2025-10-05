# /infrastructure/backend.tf

terraform {
  backend "s3" {
    # Replace with the unique S3 bucket name you created in Step 1
    bucket = "tableai-remote-state"
    key    = "global/terraform.tfstate" # The path to the state file inside the bucket
    region = "ca-central-1"

    # Replace with the DynamoDB table name you created in Step 1
    dynamodb_table = "TableAI-TerraformStateLocks"
    encrypt        = true
  }
}