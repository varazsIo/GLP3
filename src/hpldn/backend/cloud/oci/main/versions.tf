# The HPLDN serving host and everything in front of it: the OCI network and
# A1 instance, Cloudflare (DNS, TLS, Authenticated Origin Pulls with our own
# CA, WAF and rate limiting, the guard's token) and the single-use tailnet
# key. Run as hpldn-deployer (the ~/.oci/config profile cloud/oci/state made),
# by python -m hpldn.backend.cloud.oci up|status|destroy.
terraform {
  required_version = "~> 1.12.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 9.8.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.27.0"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "~> 0.29.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.4"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # State in the hpldn-tfstate bucket through OCI's S3-compatible API. The
  # rest (region, endpoints.s3, use_lockfile) is a backend config file
  # provision.py writes (0600, deleted after init); the credentials are only
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in tofu's environment, from
  # cloud/oci/state's outputs, never a file.
  backend "s3" {
    bucket                      = "hpldn-tfstate"
    key                         = "main/terraform.tfstate"
    use_path_style              = true
    skip_region_validation      = true
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }

  # Encrypted before it leaves the laptop (the bucket's CMK is a second
  # layer): AES-GCM, key from TF_VAR_state_passphrase via PBKDF2. Plaintext
  # state or plan files are refused. Passphrase rotation: see the fallback
  # note in cloud/oci/state/versions.tf.
  encryption {
    key_provider "pbkdf2" "main" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "main" {
      keys = key_provider.pbkdf2.main
    }

    state {
      method   = method.aes_gcm.main
      enforced = true
    }

    plan {
      method   = method.aes_gcm.main
      enforced = true
    }
  }
}

provider "oci" {
  config_file_profile = var.oci_profile
  region              = var.region
}

# CLOUDFLARE_API_TOKEN from the environment (the tofu token: README.md lists
# its permissions).
provider "cloudflare" {}

# TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET from the
# environment: an OAuth client limited to the auth_keys scope, not an API key.
provider "tailscale" {
  tailnet = var.tailnet
  scopes  = ["auth_keys"]
}
