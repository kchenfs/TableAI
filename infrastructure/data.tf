# Reference your EXISTING DynamoDB tables
data "aws_dynamodb_table" "menu" {
  name = "MomotaroSushiMenu_DB" # <-- Make sure this is your exact table name
}

data "aws_s3_bucket" "momotaro-assets" {
  bucket = "momotarosushi" # <-- Make sure this is your exact bucket name
}