terraform {
  required_providers {
    okta = {
      source  = "okta/okta"
      version = "~> 4.0"
    }
  }
}

provider "okta" {
  org_name  = var.okta_org_name
  base_url  = var.okta_base_url
  api_token = var.okta_api_token
}

variable "okta_org_name" {
  type        = string
  description = "Okta organization name (e.g. dev-123456)"
}

variable "okta_base_url" {
  type        = string
  description = "Okta base URL (e.g. okta.com)"
  default     = "okta.com"
}

variable "okta_api_token" {
  type        = string
  sensitive   = true
  description = "Okta API token"
}

resource "okta_group" "engineering" {
  name        = "Engineering"
  description = "Engineering department group for auto-assignment based on department profile attribute"
}

resource "okta_group_rule" "engineering_auto_assign" {
  name              = "engineering_department_auto_assign"
  status            = "ACTIVE"
  type              = "group_rule"
  group_assignments = [okta_group.engineering.id]
  expression_type   = "urn:okta:expression:GroupRule"
  expression        = "user.department == \"Engineering\""

  depends_on = [okta_group.engineering]
}

output "engineering_group_id" {
  value       = okta_group.engineering.id
  description = "ID of the Engineering group"
}

output "engineering_group_rule_id" {
  value       = okta_group_rule.engineering_auto_assign.id
  description = "ID of the engineering_department_auto_assign group rule"
}