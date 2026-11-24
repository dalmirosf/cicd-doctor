output "state_bucket_name" {
  description = "Put this in backend.hcl of the persistent and ephemeral layers."
  value       = aws_s3_bucket.state.id
}

output "region" {
  description = "Put this in backend.hcl too."
  value       = var.aws_region
}
