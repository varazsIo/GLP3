# cloud/oci/state with the OCI provider mocked: nothing is created, no
# credentials are needed (`tofu test` in cloud/oci/state; run_all.sh lint).
# Plans only: the vault, key and buckets are prevent_destroy, which a test's
# clean-up destroy would trip over.

mock_provider "oci" {
  mock_data "oci_objectstorage_namespace" {
    defaults = {
      namespace = "testnamespace"
    }
  }
}

variables {
  state_passphrase        = "correct horse battery staple, at length"
  tenancy_ocid            = "ocid1.tenancy.oc1..aaaatest"
  region                  = "uk-london-1"
  alert_email             = "ops@example.com"
  deployer_public_key_pem = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAtest\n-----END PUBLIC KEY-----\n"
}

override_resource {
  target = oci_kms_key.hpldn
  values = {
    id = "ocid1.key.oc1..cmk"
  }
}

override_resource {
  target = oci_identity_compartment.hpldn
  values = {
    id = "ocid1.compartment.oc1..hpldn"
  }
}

run "buckets_are_private_versioned_and_on_the_cmk" {
  command = plan

  assert {
    condition     = length(oci_objectstorage_bucket.buckets) == 2
    error_message = "Expected the hpldn-tfstate and hpldn-inputs buckets."
  }

  assert {
    condition     = alltrue([for b in oci_objectstorage_bucket.buckets : b.access_type == "NoPublicAccess"])
    error_message = "Every bucket must be NoPublicAccess."
  }

  assert {
    condition     = alltrue([for b in oci_objectstorage_bucket.buckets : b.versioning == "Enabled"])
    error_message = "Every bucket must be versioned."
  }

  assert {
    condition     = alltrue([for b in oci_objectstorage_bucket.buckets : b.kms_key_id == "ocid1.key.oc1..cmk"])
    error_message = "Every bucket must be encrypted with the customer-managed key."
  }

  assert {
    condition     = alltrue([for p in oci_objectstorage_object_lifecycle_policy.buckets : alltrue([for r in p.rules : r.target == "previous-object-versions" && r.action == "DELETE" && r.time_amount == "90"])])
    error_message = "Superseded versions must be deleted after 90 days."
  }
}

run "the_key_is_hsm_aes256_and_only_storage_may_use_it" {
  command = plan

  assert {
    condition     = oci_kms_key.hpldn.protection_mode == "HSM" && oci_kms_key.hpldn.key_shape[0].algorithm == "AES" && oci_kms_key.hpldn.key_shape[0].length == 32
    error_message = "The key must be AES-256 in the HSM."
  }

  assert {
    condition     = oci_kms_vault.hpldn.vault_type == "DEFAULT"
    error_message = "The vault must be the free DEFAULT type."
  }

  assert {
    condition     = alltrue([for s in oci_identity_policy.services_use_key.statements : strcontains(s, "where target.key.id = 'ocid1.key.oc1..cmk'")])
    error_message = "The storage services may use this key only."
  }
}

run "the_state_key_reaches_the_state_bucket_only" {
  command = plan

  assert {
    condition     = alltrue([for s in oci_identity_policy.tfstate.statements : strcontains(s, "target.bucket.name = 'hpldn-tfstate'")])
    error_message = "Every hpldn-tfstate statement must be limited to the state bucket."
  }
}

run "the_deployer_is_limited_to_the_compartment" {
  command = plan

  assert {
    condition     = contains(oci_identity_policy.deployer.statements, "Allow group hpldn-deployer to manage all-resources in compartment id ocid1.compartment.oc1..hpldn")
    error_message = "The deployer manages the hpldn compartment."
  }

  assert {
    condition     = length([for s in oci_identity_policy.deployer.statements : s if strcontains(s, "manage") && strcontains(s, "in tenancy")]) == 0
    error_message = "The deployer must not manage anything tenancy-wide."
  }

  assert {
    condition     = !strcontains(oci_identity_api_key.deployer.key_value, "PRIVATE")
    error_message = "Only a public key is given to the deployer."
  }
}

run "the_quota_allows_free_a1_only" {
  command = plan

  assert {
    condition = alltrue([for s in [
      "zero compute-core quotas in compartment hpldn",
      "set compute-core quota standard-a1-core-count to 4 in compartment hpldn",
      "set compute-core quota standard-a1-core-regional-count to 4 in compartment hpldn",
      "zero compute-memory quotas in compartment hpldn",
      "set compute-memory quota standard-a1-memory-count to 24 in compartment hpldn",
      "set compute-memory quota standard-a1-memory-regional-count to 24 in compartment hpldn",
      "set block-storage quota total-storage-gb to 200 in compartment hpldn",
      "zero load-balancer quotas in compartment hpldn",
      "zero database quotas in compartment hpldn",
    ] : contains(oci_limits_quota.hpldn.statements, s)])
    error_message = "The quota must zero every paid shape and service in the compartment."
  }
}

run "both_budget_alerts_exist" {
  command = plan

  assert {
    condition     = oci_budget_budget.hpldn.target_type == "COMPARTMENT" && oci_budget_budget.hpldn.reset_period == "MONTHLY"
    error_message = "The budget targets the compartment, monthly."
  }

  assert {
    condition     = oci_budget_alert_rule.actual.type == "ACTUAL" && oci_budget_alert_rule.actual.threshold == 1
    error_message = "An alert on the first unit of actual spend."
  }

  assert {
    condition     = oci_budget_alert_rule.forecast.type == "FORECAST" && oci_budget_alert_rule.forecast.threshold == 15
    error_message = "An alert on a forecast over the budget."
  }
}

run "secret_outputs_are_sensitive" {
  command = plan

  assert {
    condition     = issensitive(output.s3_secret_key) && issensitive(output.s3_access_key_id)
    error_message = "The S3 credentials must be sensitive outputs."
  }
}

run "a_short_passphrase_is_refused" {
  command = plan

  variables {
    state_passphrase = "too short"
  }

  expect_failures = [var.state_passphrase]
}

run "a_private_key_is_refused" {
  command = plan

  variables {
    deployer_public_key_pem = "-----BEGIN PUBLIC KEY-----\n-----BEGIN PRIVATE KEY-----\n"
  }

  expect_failures = [var.deployer_public_key_pem]
}
