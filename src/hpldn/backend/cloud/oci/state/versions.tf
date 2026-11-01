# The one-time foundation for the HPLDN deployment, applied once by the
# tenancy administrator (python -m hpldn.backend.cloud.oci state, or tofu
# apply here). Its state stays local (terraform.tfstate here, gitignored),
# encrypted; provision.py copies the encrypted file to
# hpldn-tfstate/bootstrap/ as a backup.
terraform {
  required_version = "~> 1.12.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 9.8.0"
    }
  }

  # State and plan files are encrypted at rest with AES-GCM under a key
  # derived (PBKDF2) from TF_VAR_state_passphrase (>= 32 characters, from the
  # operator's password manager). `enforced` refuses to write either in
  # plaintext, ever.
  encryption {
    key_provider "pbkdf2" "main" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "main" {
      keys = key_provider.pbkdf2.main
    }

    # Rotating the passphrase: put the old one in a second key provider,
    # point a `fallback` at a method using it, apply once with both
    # passphrases set, then remove the fallback. For example:
    #
    #   key_provider "pbkdf2" "old" { passphrase = var.old_state_passphrase }
    #   method "aes_gcm" "old" { keys = key_provider.pbkdf2.old }
    #   state {
    #     method   = method.aes_gcm.main
    #     enforced = true
    #     fallback { method = method.aes_gcm.old }
    #   }

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

# The tenancy administrator's own profile in ~/.oci/config. Identity
# resources (compartment, users, groups, policies) live in the home region,
# so `region` must be the tenancy's home region.
provider "oci" {
  config_file_profile = var.oci_profile
  region              = var.region
}
