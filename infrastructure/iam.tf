# Define the IAM Role for unauthenticated Cognito users
resource "aws_iam_role" "cognito_unauth_role" {
  name = "TableAICognitoUnauthRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow",
        Principal = {
          "Federated" = "cognito-identity.amazonaws.com"
        },
        Action = "sts:AssumeRoleWithWebIdentity",
        Condition = {
          "StringEquals" = {
            "cognito-identity.amazonaws.com:aud" = aws_cognito_identity_pool.tableai_pool.id
          }
        }
      }
    ]
  })
}


