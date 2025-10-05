# Create the Cognito Identity Pool for guest access
resource "aws_cognito_identity_pool" "tableai_pool" {
  identity_pool_name               = "TableAIGuestPool"
  allow_unauthenticated_identities = true

  tags = {
    Project = "TableAI"
  }
}

# Connect the IAM Role to the Cognito Identity Pool
resource "aws_cognito_identity_pool_roles_attachment" "pool_roles" {
  identity_pool_id = aws_cognito_identity_pool.tableai_pool.id
  roles = {
    "unauthenticated" = aws_iam_role.cognito_unauth_role.arn
  }
}