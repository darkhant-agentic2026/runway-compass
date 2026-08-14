# tflint runs in the `tf:` CI job (docs/07-infra-deploy.md#ciyml-every-pr).
#
# The google ruleset is deliberately NOT enabled with `deep_check`: deep checks call the
# GCP API, which would mean this job needed a credential and could reach a real project.
# The static rules catch the things that actually bite — unused declarations, missing
# types on variables, and module-source hygiene.

config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

rule "terraform_required_version" {
  enabled = true
}

rule "terraform_required_providers" {
  enabled = true
}

rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}

rule "terraform_naming_convention" {
  enabled = true
  format  = "snake_case"
}

rule "terraform_unused_declarations" {
  enabled = true
}

rule "terraform_typed_variables" {
  enabled = true
}
