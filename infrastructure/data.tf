# Reference your EXISTING DynamoDB tables
data "aws_dynamodb_table" "menu" {
  name = "Momotaro-Dashboard-Menu" # <-- Make sure this is your exact table name
}

data "aws_dynamodb_table" "orders" {
  name = "momotaroOrdersDatabase" # <-- Make sure this is your exact table name
}
