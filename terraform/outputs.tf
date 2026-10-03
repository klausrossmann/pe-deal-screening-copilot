output "ec2_public_ip" {
  value = aws_instance.app.public_ip
}

output "rds_endpoint" {
  description = "Host:port to use in POSTGRES_URL (append /pe_screening?sslmode=require)."
  value       = aws_db_instance.this.endpoint
}

output "streamlit_url" {
  value = "http://${aws_instance.app.public_ip}:8501"
}
