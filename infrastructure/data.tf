# Reference your EXISTING DynamoDB tables
data "aws_dynamodb_table" "menu" {
  name = "MomotaroSushiMenu_DB" # <-- Make sure this is your exact table name
}

