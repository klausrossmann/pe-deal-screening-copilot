# Resolves the latest PostgreSQL 16.x minor version available in this region/account
# instead of hardcoding one that may not (yet, or any longer) be offered.
data "aws_rds_engine_version" "postgres" {
  engine             = "postgres"
  preferred_versions = ["16.*"]
}

resource "aws_db_instance" "this" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = data.aws_rds_engine_version.postgres.version

  instance_class         = var.rds_instance_class
  allocated_storage      = var.rds_allocated_storage
  db_name                = var.db_name
  username               = var.db_username
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  publicly_accessible     = false
  multi_az                = false
  storage_encrypted       = true
  skip_final_snapshot     = true
  backup_retention_period = 0

  tags = {
    Project = var.project_name
  }
}
