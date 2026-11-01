variable "state_passphrase" {
  description = "Passphrase the state and plan encryption key is derived from (TF_VAR_state_passphrase; never in a file)."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.state_passphrase) >= 32
    error_message = "The state passphrase must be at least 32 characters."
  }
}

variable "oci_profile" {
  description = "The ~/.oci/config profile of a tenancy administrator."
  type        = string
  default     = "DEFAULT"
}

variable "tenancy_ocid" {
  description = "The tenancy's OCID: the compartment, users, groups, budget and quota are created in it."
  type        = string

  validation {
    condition     = can(regex("^ocid1\\.tenancy\\.", var.tenancy_ocid))
    error_message = "tenancy_ocid must be a tenancy OCID (ocid1.tenancy...)."
  }
}

variable "region" {
  description = "The tenancy's home region, e.g. uk-london-1."
  type        = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+-[0-9]+$", var.region))
    error_message = "region must look like uk-london-1."
  }
}

variable "compartment_name" {
  description = "The compartment everything of HPLDN's lives in."
  type        = string
  default     = "hpldn"
}

variable "alert_email" {
  description = "Where the budget alerts go; also the contact address on the hpldn-tfstate and hpldn-deployer users."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an e-mail address."
  }
}

variable "budget_actual_alert" {
  description = "Alert as soon as actual spend in the compartment reaches this (the tenancy's currency, e.g. GBP)."
  type        = number
  default     = 1
}

variable "budget_amount" {
  description = "The monthly budget, and the forecast alert threshold (the tenancy's currency)."
  type        = number
  default     = 15
}

variable "deployer_public_key_pem" {
  description = "The PEM public key of the hpldn-deployer API key pair (the private half stays in ~/.oci on the laptop)."
  type        = string

  validation {
    condition     = can(regex("^-----BEGIN PUBLIC KEY-----", trimspace(var.deployer_public_key_pem))) && !can(regex("PRIVATE KEY", var.deployer_public_key_pem))
    error_message = "deployer_public_key_pem must be a PEM public key, and never a private key."
  }
}

variable "noncurrent_version_days" {
  description = "Days a superseded object version is kept in the state and inputs buckets."
  type        = number
  default     = 90
}
