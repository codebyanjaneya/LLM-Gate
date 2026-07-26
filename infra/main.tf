# main.tf - Terraform for a single EC2 instance that runs our sample Flask app.
#
# This represents "AI-generated infrastructure". It contains TWO intentional
# misconfigurations so our OPA policies have something concrete to catch:
#   1. A security group open to 0.0.0.0/0 on ALL ports (ingress + egress).
#   2. A root EBS volume with encryption DISABLED.
#
# Do NOT copy this into a real environment - it is insecure on purpose.

terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# --- Inputs -----------------------------------------------------------------

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type (t2.micro is free-tier eligible)"
  type        = string
  default     = "t2.micro"
}

variable "ami_id" {
  description = "AMI id for the instance (Amazon Linux 2 recommended)"
  type        = string
  default     = "ami-0c101f26f147fa7fd" # Amazon Linux 2 (us-east-1) - update as needed
}

provider "aws" {
  region = var.aws_region
}

# ---------------------------------------------------------------------------
# Security group with restricted ingress and egress rules.
# ---------------------------------------------------------------------------
resource "aws_security_group" "app_sg" {
  name        = "llm-gate-demo-sg"
  description = "Demo SG - restricted access"

  ingress {
    description = "Allow inbound SSH from a specific IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"] 
  }

  ingress {
    description = "Allow inbound HTTP from a specific IP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }

  egress {
    description = "Allow outbound HTTP and HTTPS to a specific IP"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["10.0.0.0/16"]
  }

  tags = {
    Project = "llm-gate"
    Note    = "restricted-access"
  }
}

# ---------------------------------------------------------------------------
# EC2 instance that would run the Flask app (bootstrapped via user_data).
# ---------------------------------------------------------------------------
resource "aws_instance" "app_server" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.app_sg.id]

  # Minimal first-boot bootstrap: install Python + Flask.
  # (In the full pipeline the app code is copied here and started.)
  user_data = <<-EOF
    #!/bin/bash
    yum update -y
    yum install -y python3 git
    pip3 install flask
  EOF

  root_block_device {
    volume_size = 8
    encrypted   = true 
  }

  tags = {
    Project = "llm-gate"
    Name    = "llm-gate-demo-app"
  }
}

# --- Outputs (used by the deploy + Selenium test stages) --------------------

output "instance_public_ip" {
  description = "Public IP the Selenium tests use to reach the app"
  value       = aws_instance.app_server.public_ip
}

output "app_url" {
  description = "Base URL of the deployed Flask app"
  value       = "http://${aws_instance.app_server.public_ip}:80"
}
