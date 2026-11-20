variable "region" { type = string }
variable "name" {
  type    = string
  default = "graph-platform"
}
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "image_tag" {
  type    = string
  default = "latest"
}
variable "schedule" {
  type    = string
  default = "rate(1 hour)"
}
variable "monthly_budget_usd" { type = number }
variable "alert_email" { type = string }
variable "neo4j_instance_type" {
  type    = string
  default = "r7g.xlarge"
}
variable "neo4j_disk_gb" {
  type    = number
  default = 200
}
