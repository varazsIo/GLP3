variable "state_passphrase" {
  description = "Passphrase the state and plan encryption key is derived from (TF_VAR_state_passphrase)."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.state_passphrase) >= 32
    error_message = "The state passphrase must be at least 32 characters."
  }
}

variable "oci_profile" {
  description = "The hpldn-deployer profile in ~/.oci/config."
  type        = string
  default     = "hpldn-deployer"
}

variable "region" {
  description = "The OCI region, e.g. uk-london-1."
  type        = string
}

variable "tenancy_ocid" {
  description = "The tenancy (platform images are listed from it)."
  type        = string
}

variable "compartment_id" {
  description = "The hpldn compartment (cloud/oci/state output compartment_id)."
  type        = string

  validation {
    condition     = can(regex("^ocid1\\.compartment\\.", var.compartment_id))
    error_message = "compartment_id must be a compartment OCID."
  }
}

variable "kms_key_id" {
  description = "The customer-managed key (cloud/oci/state output kms_key_id): the boot volume is encrypted with it."
  type        = string

  validation {
    condition     = can(regex("^ocid1\\.key\\.", var.kms_key_id))
    error_message = "kms_key_id must be a Vault key OCID."
  }
}

variable "domain" {
  description = "The site's host name, e.g. hpldn.example.co.uk (a proxied A record in zone_name)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", var.domain))
    error_message = "domain must be a lower-case DNS name."
  }
}

variable "zone_name" {
  description = "The Cloudflare zone the domain is in, e.g. example.co.uk."
  type        = string
}

variable "cloudflare_account_id" {
  description = "The Cloudflare account (the guard's IP list and token live there)."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id is 32 hex characters."
  }
}

variable "tailnet" {
  description = "The tailnet (\"-\" is the OAuth client's own)."
  type        = string
  default     = "-"
}

variable "ssh_public_key" {
  description = "The operator's ed25519 public key (ubuntu's authorized_keys)."
  type        = string

  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/]+={0,2}( .*)?$", trimspace(var.ssh_public_key)))
    error_message = "ssh_public_key must be one ssh-ed25519 public key."
  }
}

variable "ocpus" {
  description = "A1 OCPUs. Always Free covers 4 per tenancy."
  type        = number
  default     = 4

  validation {
    condition     = var.ocpus >= 1 && var.ocpus <= 4 && floor(var.ocpus) == var.ocpus
    error_message = "ocpus must be a whole number from 1 to 4 (the Always Free A1 allowance)."
  }
}

variable "memory_gb" {
  description = "A1 memory in GB. Always Free covers 24 per tenancy."
  type        = number
  default     = 24

  validation {
    condition     = var.memory_gb >= 6 && var.memory_gb <= 24
    error_message = "memory_gb must be from 6 to 24 (the Always Free A1 allowance)."
  }
}

variable "boot_gb" {
  description = "Boot volume size in GB. Always Free covers 200 GB of block storage in all."
  type        = number
  default     = 100

  validation {
    condition     = var.boot_gb >= 50 && var.boot_gb <= 200
    error_message = "boot_gb must be from 50 to 200 (the Always Free block storage allowance)."
  }
}

variable "ad_index" {
  description = "Which availability domain (0-based); provision.py moves on when one is out of A1 capacity."
  type        = number
  default     = 0
}

variable "tailscale_direct" {
  description = "Admit Tailscale's direct UDP (41641) so admin traffic need not go through a relay."
  type        = bool
  default     = true
}

variable "bootstrap_ssh" {
  description = "Break-glass only: open tcp/22 to admin_cidr. Every normal session is SSH over the tailnet."
  type        = bool
  default     = false
}

variable "admin_cidr" {
  description = "The single address break-glass SSH is opened to (with bootstrap_ssh)."
  type        = string
  default     = "192.0.2.1/32"

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0)) && tonumber(split("/", var.admin_cidr)[1]) >= 24
    error_message = "admin_cidr must be one address or a small range (/24 or narrower), never the internet."
  }
}

variable "api_rate_limit" {
  description = "Cloudflare's edge rate limit on /api/: requests per client IP per 10 seconds (free plan: 10 s period)."
  type        = number
  default     = 100
}

variable "origin_cert_days" {
  description = "Validity of the Cloudflare Origin CA certificate (re-issued by provision.py up --renew-certs)."
  type        = number
  default     = 365

  validation {
    condition     = contains([7, 30, 90, 365, 730, 1095, 5475], var.origin_cert_days)
    error_message = "origin_cert_days must be one Cloudflare accepts: 7, 30, 90, 365, 730, 1095 or 5475."
  }
}

variable "firewall_entrypoint_id" {
  description = "Id of the zone's existing http_request_firewall_custom ruleset, when it has one (provision.py looks it up); empty creates it."
  type        = string
  default     = ""
}

variable "redirect_entrypoint_id" {
  description = "Id of the zone's existing http_request_dynamic_redirect ruleset, when it has one (provision.py looks it up); empty creates it."
  type        = string
  default     = ""
}

variable "cache_entrypoint_id" {
  description = "Id of the zone's existing http_request_cache_settings ruleset, when it has one (provision.py looks it up); empty creates it."
  type        = string
  default     = ""
}
