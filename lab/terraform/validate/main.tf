# Case tf-validate: terraform_data has no argument named "bogus".
resource "terraform_data" "lab" {
  input = "lab"
  bogus = 1
}
