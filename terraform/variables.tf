variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-central-1"
}

variable "project_name" {
  description = "Prefix used to name/tag all resources."
  type        = string
  default     = "pe-screening"
}

variable "ec2_instance_type" {
  description = "Free-tier eligible EC2 instance type (check your region/account: t2.micro or t3.micro)."
  type        = string
  default     = "t3.micro"
}

variable "rds_instance_class" {
  description = "Free-tier eligible RDS instance class (check your region/account: db.t3.micro or db.t4g.micro)."
  type        = string
  default     = "db.t3.micro"
}

variable "rds_allocated_storage" {
  description = "RDS storage in GB (20 GB is the free-tier ceiling)."
  type        = number
  default     = 20
}

variable "db_name" {
  type    = string
  default = "pe_screening"
}

variable "db_username" {
  type    = string
  default = "postgres"
}

variable "db_password" {
  description = "RDS master password. Pass via terraform.tfvars (gitignored) or TF_VAR_db_password, never commit it."
  type        = string
  sensitive   = true
}

variable "key_pair_name" {
  description = "Name of an EC2 key pair that already exists in this region (create with `aws ec2 create-key-pair`)."
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH into the EC2 instance, e.g. \"YOUR.IP.ADDR.ESS/32\". Never leave this as 0.0.0.0/0."
  type        = string
}

variable "git_repo_url" {
  description = "Git URL the EC2 instance clones on first boot. Leave empty to skip and upload the repo some other way (e.g. scp)."
  type        = string
  default     = ""
}

variable "budget_alert_email" {
  description = "Email address to notify when the monthly AWS Budget threshold is crossed."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly AWS Budget limit in USD that triggers the alert email."
  type        = number
  default     = 5
}
