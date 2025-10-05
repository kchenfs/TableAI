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

# Create the IAM policy from the document defined in data.tf
resource "aws_iam_policy" "cognito_unauth_policy" {
  name   = "TableAICognitoUnauthPolicy"
  policy = data.aws_iam_policy_document.cognito_unauth_policy_doc.json
}

# Attach the policy to the role
resource "aws_iam_role_policy_attachment" "cognito_unauth_attach" {
  role       = aws_iam_role.cognito_unauth_role.name
  policy_arn = aws_iam_policy.cognito_unauth_policy.arn
}