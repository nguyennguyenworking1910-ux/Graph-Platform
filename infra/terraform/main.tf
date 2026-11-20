terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { project = var.name } }
}

data "aws_caller_identity" "me" {}

locals {
  # tenants and domains come from the platform's own onboarding contracts
  domain_files = fileset("${path.module}/../../conf/tenants", "*/domains/*.yaml")
  domains = { for f in local.domain_files : "${split("/", f)[0]}/${trimsuffix(basename(f), ".yaml")}" => merge(
    yamldecode(file("${path.module}/../../conf/tenants/${f}")), { tenant = split("/", f)[0] }
  ) }
  tenants = toset([for d in local.domains : d.tenant])
  bucket  = "${var.name}-${data.aws_caller_identity.me.account_id}-${var.region}"
}

# ---------------------------------------------------------------- storage
resource "aws_s3_bucket" "data" {
  bucket = local.bucket
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_notification" "data" {
  bucket      = aws_s3_bucket.data.id
  eventbridge = true
}

# ---------------------------------------------------------------- image
resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_iam_role" "build" {
  name = "${var.name}-build"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" }
  }] })
}

resource "aws_iam_role_policy" "build" {
  role = aws_iam_role.build.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage"], Resource = aws_ecr_repository.app.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.data.arn}/_build/*" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "*" },
  ] })
}

# The dev machine has no Docker: the image is built in AWS from a source zip uploaded to _build/source.zip.
resource "aws_codebuild_project" "image" {
  name         = "${var.name}-image"
  service_role = aws_iam_role.build.arn
  artifacts { type = "NO_ARTIFACTS" }
  environment {
    compute_type    = "BUILD_GENERAL1_MEDIUM"
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true
    environment_variable {
      name  = "REPO"
      value = aws_ecr_repository.app.repository_url
    }
    environment_variable {
      name  = "TAG"
      value = var.image_tag
    }
  }
  source {
    type      = "S3"
    location  = "${aws_s3_bucket.data.bucket}/_build/source.zip"
    buildspec = file("${path.module}/../buildspec.yml")
  }
}

# ---------------------------------------------------------------- task
resource "aws_ecs_cluster" "main" {
  name = var.name
}

resource "aws_cloudwatch_log_group" "task" {
  name              = "/${var.name}/supervisor"
  retention_in_days = 90
}

resource "aws_iam_role" "execution" {
  name = "${var.name}-execution"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ecs-tasks.amazonaws.com" }
  }] })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# One task role per tenant: it can touch that tenant's prefix and nothing else.
resource "aws_iam_role" "tenant" {
  for_each = local.tenants
  name     = "${var.name}-${replace(each.key, "_", "-")}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ecs-tasks.amazonaws.com" }
  }] })
}

resource "aws_iam_role_policy" "tenant" {
  for_each = local.tenants
  role     = aws_iam_role.tenant[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.data.arn}/${each.key}/*" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.data.arn,
    Condition = { StringLike = { "s3:prefix" = ["${each.key}/*"] } } },
    { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = "*" },
  ] })
}

# One read role per business domain: its own landing/canonical data plus the domains that list it as a reader.
resource "aws_iam_role" "domain" {
  for_each = local.domains
  name     = "${var.name}-${replace(replace(each.key, "/", "-"), "_", "-")}-reader"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.me.account_id}:root" }
  }] })
}

resource "aws_iam_role_policy" "domain" {
  for_each = local.domains
  role     = aws_iam_role.domain[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "s3:GetObject",
    Resource = flatten([for k, d in local.domains : [
      "${aws_s3_bucket.data.arn}/${d.tenant}/data/landing/${d.domain}/*",
      "${aws_s3_bucket.data.arn}/${d.tenant}/data/canonical/${d.domain}/*",
    ] if d.tenant == each.value.tenant && (d.domain == each.value.domain || contains(try(d.readers, []), each.value.domain))])
  }] })
}

resource "aws_ecs_task_definition" "supervisor" {
  for_each                 = local.tenants
  family                   = "${var.name}-${replace(each.key, "_", "-")}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 8192
  memory                   = 61440
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.tenant[each.key].arn
  ephemeral_storage { size_in_gib = 100 }
  container_definitions = jsonencode([{
    name      = "supervisor"
    image     = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
    essential = true
    environment = [
      { name = "TENANT", value = each.key },
      { name = "DATA_BUCKET", value = aws_s3_bucket.data.bucket },
      { name = "AWS_REGION", value = var.region },
    ]
    logConfiguration = { logDriver = "awslogs", options = {
      "awslogs-group" = aws_cloudwatch_log_group.task.name, "awslogs-region" = var.region, "awslogs-stream-prefix" = each.key
    } }
  }])
}

resource "aws_security_group" "task" {
  name   = "${var.name}-task"
  vpc_id = var.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------------------------------------------------------------- triggers: new landing data, and a schedule
resource "aws_iam_role" "events" {
  name = "${var.name}-events"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = ["events.amazonaws.com", "scheduler.amazonaws.com"] }
  }] })
}

resource "aws_iam_role_policy" "events" {
  role = aws_iam_role.events.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ecs:RunTask", Resource = [for t in aws_ecs_task_definition.supervisor : t.arn_without_revision] },
    { Effect = "Allow", Action = "iam:PassRole", Resource = concat([aws_iam_role.execution.arn], [for r in aws_iam_role.tenant : r.arn]) },
  ] })
}

resource "aws_cloudwatch_event_rule" "landing" {
  for_each = local.tenants
  name     = "${var.name}-${replace(each.key, "_", "-")}-landing"
  event_pattern = jsonencode({
    source = ["aws.s3"], "detail-type" = ["Object Created"],
    detail = { bucket = { name = [aws_s3_bucket.data.bucket] }, object = { key = [{ prefix = "${each.key}/data/landing/" }] } }
  })
}

resource "aws_cloudwatch_event_target" "landing" {
  for_each = local.tenants
  rule     = aws_cloudwatch_event_rule.landing[each.key].name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events.arn
  ecs_target {
    task_definition_arn = aws_ecs_task_definition.supervisor[each.key].arn_without_revision
    launch_type         = "FARGATE"
    network_configuration {
      subnets         = var.private_subnet_ids
      security_groups = [aws_security_group.task.id]
    }
  }
}

resource "aws_scheduler_schedule" "supervisor" {
  for_each            = local.tenants
  name                = "${var.name}-${replace(each.key, "_", "-")}"
  schedule_expression = var.schedule
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.events.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.supervisor[each.key].arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets         = var.private_subnet_ids
        security_groups = [aws_security_group.task.id]
      }
    }
  }
}

# ---------------------------------------------------------------- cost guard
resource "aws_budgets_budget" "monthly" {
  name         = var.name
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$${var.name}"]
  }
  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}

# ---------------------------------------------------------------- graph database: Neo4j on one EC2 instance
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "neo4j" {
  name   = "${var.name}-neo4j"
  vpc_id = var.vpc_id
  ingress {
    from_port       = 7687
    to_port         = 7687
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_secretsmanager_secret" "neo4j" {
  name = "${var.name}/neo4j-password"
}

resource "aws_instance" "neo4j" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.neo4j_instance_type
  subnet_id              = var.private_subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.neo4j.id]
  root_block_device {
    volume_size = var.neo4j_disk_gb
    volume_type = "gp3"
    encrypted   = true
  }
  user_data = <<-EOT
    #!/bin/bash
    rpm --import https://debian.neo4j.com/neotechnology.gpg.key
    printf '[neo4j]\nname=Neo4j\nbaseurl=https://yum.neo4j.com/stable/5\nenabled=1\ngpgcheck=1\n' > /etc/yum.repos.d/neo4j.repo
    NEO4J_ACCEPT_LICENSE_AGREEMENT=yes dnf install -y neo4j
    sed -i 's/#server.default_listen_address=0.0.0.0/server.default_listen_address=0.0.0.0/' /etc/neo4j/neo4j.conf
    systemctl enable --now neo4j
  EOT
  tags      = { Name = "${var.name}-neo4j" }
}

output "data_bucket" { value = aws_s3_bucket.data.bucket }
output "image_repository" { value = aws_ecr_repository.app.repository_url }
output "neo4j_private_ip" { value = aws_instance.neo4j.private_ip }
