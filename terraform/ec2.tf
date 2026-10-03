data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.amazon_linux.id
  instance_type          = var.ec2_instance_type
  key_name               = var.key_pair_name
  vpc_security_group_ids = [aws_security_group.ec2.id]

  # RDS isn't reachable until it exists; this avoids the instance booting before the DB is up.
  depends_on = [aws_db_instance.this]

  user_data = templatefile("${path.module}/templates/user-data.sh.tftpl", {
    git_repo_url = var.git_repo_url
  })

  tags = {
    Name    = "${var.project_name}-app"
    Project = var.project_name
  }
}
