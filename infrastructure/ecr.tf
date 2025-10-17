# /infrastructure/ecr.tf

# ------------------------------------------------------------------------------
# ECR REPOSITORY for Lambda Container Image
# ------------------------------------------------------------------------------
resource "aws_ecr_repository" "momotaro_lex_bot" {
  name                 = "momotaro-lex-bot"
  image_tag_mutability = "MUTABLE" # Allows overwriting the 'latest' tag

  image_scanning_configuration {
    scan_on_push = true # Good security practice
  }

  tags = {
    Name    = "MomotaroLexBot"
    Project = "TableAI"
  }
}

