locals {
  tfstate_bucket = "hpldn-tfstate"
  inputs_bucket  = "hpldn-inputs"
  compartment_id = oci_identity_compartment.hpldn.id
  # Everything this module makes is tagged, so the console can list it.
  tags = { project = "hpldn", managed_by = "opentofu-state" }
}

data "oci_objectstorage_namespace" "main" {
  compartment_id = var.tenancy_ocid
}

# ---------------------------------------------------------------------------
# The compartment every HPLDN resource lives in.
# ---------------------------------------------------------------------------
resource "oci_identity_compartment" "hpldn" {
  compartment_id = var.tenancy_ocid
  name           = var.compartment_name
  description    = "HPLDN serving host, its network, state and inputs (OpenTofu)"
  enable_delete  = false
  freeform_tags  = local.tags
}

# ---------------------------------------------------------------------------
# Vault and the customer-managed key: the boot volume and both buckets are
# encrypted with it. A DEFAULT (virtual) vault is free, and so are its first
# 20 key versions: rotating once a year (oci kms management key rotate,
# README.md) stays well inside that.
# ---------------------------------------------------------------------------
resource "oci_kms_vault" "hpldn" {
  compartment_id = local.compartment_id
  display_name   = "hpldn"
  vault_type     = "DEFAULT"
  freeform_tags  = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_kms_key" "hpldn" {
  compartment_id      = local.compartment_id
  display_name        = "hpldn-cmk"
  management_endpoint = oci_kms_vault.hpldn.management_endpoint
  protection_mode     = "HSM"
  freeform_tags       = local.tags

  key_shape {
    algorithm = "AES"
    length    = 32
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Block storage and Object Storage may use THIS key and no other.
resource "oci_identity_policy" "services_use_key" {
  compartment_id = local.compartment_id
  name           = "hpldn-services-use-cmk"
  description    = "Block and Object Storage may use the HPLDN key only"
  statements = [
    "Allow service blockstorage to use keys in compartment id ${local.compartment_id} where target.key.id = '${oci_kms_key.hpldn.id}'",
    "Allow service objectstorage-${var.region} to use keys in compartment id ${local.compartment_id} where target.key.id = '${oci_kms_key.hpldn.id}'",
  ]
  freeform_tags = local.tags
}


resource "oci_identity_policy" "object_storage_lifecycle_policy" {
  compartment_id = var.tenancy_ocid 
  
  name        = "Allow-ObjectStorage-Lifecycle-London"
  description = "Grants OCI Object Storage service access to manage lifecycles in London region."

  statements = [
    "Allow service objectstorage-uk-london-1 to manage object-family in tenancy"
  ]
}

# ---------------------------------------------------------------------------
# Buckets: the encrypted OpenTofu state of main/ and the migration inputs
# (age-encrypted on the laptop before upload). Private, versioned, the CMK
# on top; superseded versions go after `noncurrent_version_days`.
# ---------------------------------------------------------------------------
resource "oci_objectstorage_bucket" "buckets" {
  for_each = toset([local.tfstate_bucket, local.inputs_bucket])

  compartment_id = local.compartment_id
  namespace      = data.oci_objectstorage_namespace.main.namespace
  name           = each.key
  access_type    = "NoPublicAccess"
  versioning     = "Enabled"
  kms_key_id     = oci_kms_key.hpldn.id
  storage_tier   = "Standard"
  freeform_tags  = local.tags

  depends_on = [oci_identity_policy.services_use_key]

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_objectstorage_object_lifecycle_policy" "buckets" {
  for_each = oci_objectstorage_bucket.buckets

  namespace = data.oci_objectstorage_namespace.main.namespace
  bucket    = each.value.name

  rules {
    name        = "delete-noncurrent-versions"
    action      = "DELETE"
    target      = "previous-object-versions"
    is_enabled  = true
    time_amount = tostring(var.noncurrent_version_days)
    time_unit   = "DAYS"
  }
}

# ---------------------------------------------------------------------------
# hpldn-tfstate: the S3-compatible key main/'s backend uses. It can read and
# write objects in the state bucket and nothing else.
# ---------------------------------------------------------------------------
resource "oci_identity_user" "tfstate" {
  compartment_id = var.tenancy_ocid
  name           = "hpldn-tfstate"
  description    = "OpenTofu state backend for HPLDN main/ (S3 compatibility key only)"
  freeform_tags  = local.tags
  email          = var.alert_email
}

resource "oci_identity_group" "tfstate" {
  compartment_id = var.tenancy_ocid
  name           = "hpldn-tfstate"
  description    = "May use the hpldn-tfstate bucket only"
  freeform_tags  = local.tags
}

resource "oci_identity_user_group_membership" "tfstate" {
  user_id  = oci_identity_user.tfstate.id
  group_id = oci_identity_group.tfstate.id
}

resource "oci_identity_policy" "tfstate" {
  compartment_id = local.compartment_id
  name           = "hpldn-tfstate"
  description    = "The state backend's key reaches the state bucket only"
  statements = [
    "Allow group ${oci_identity_group.tfstate.name} to read buckets in compartment id ${local.compartment_id} where target.bucket.name = '${local.tfstate_bucket}'",
    "Allow group ${oci_identity_group.tfstate.name} to manage objects in compartment id ${local.compartment_id} where target.bucket.name = '${local.tfstate_bucket}'",
  ]
  freeform_tags = local.tags
}

resource "oci_identity_customer_secret_key" "tfstate" {
  user_id      = oci_identity_user.tfstate.id
  display_name = "hpldn-tfstate-s3"
}

# ---------------------------------------------------------------------------
# hpldn-deployer: the identity main/ runs as. It manages what is in the
# hpldn compartment and nothing outside it; quotas (below) stop it creating
# anything that is not free. Only its PUBLIC key is given here.
# ---------------------------------------------------------------------------
resource "oci_identity_user" "deployer" {
  compartment_id = var.tenancy_ocid
  name           = "hpldn-deployer"
  description    = "Runs HPLDN's OpenTofu main/ (compartment hpldn only)"
  freeform_tags  = local.tags
  email          = var.alert_email
}

resource "oci_identity_group" "deployer" {
  compartment_id = var.tenancy_ocid
  name           = "hpldn-deployer"
  description    = "Manages the hpldn compartment only"
  freeform_tags  = local.tags
}

resource "oci_identity_user_group_membership" "deployer" {
  user_id  = oci_identity_user.deployer.id
  group_id = oci_identity_group.deployer.id
}

resource "oci_identity_policy" "deployer" {
  # In the tenancy: it grants on the compartment and reads the namespace.
  compartment_id = var.tenancy_ocid
  name           = "hpldn-deployer"
  description    = "HPLDN deployer: the hpldn compartment, and the Object Storage namespace"
  statements = [
    "Allow group ${oci_identity_group.deployer.name} to manage all-resources in compartment id ${local.compartment_id}",
    "Allow group ${oci_identity_group.deployer.name} to read objectstorage-namespaces in tenancy",
  ]
  freeform_tags = local.tags
}

resource "oci_identity_api_key" "deployer" {
  user_id   = oci_identity_user.deployer.id
  key_value = trimspace(var.deployer_public_key_pem)
}

# ---------------------------------------------------------------------------
# Spend: a monthly budget on the compartment, alerting on the first unit of
# actual spend and on a forecast over the budget.
# ---------------------------------------------------------------------------
resource "oci_budget_budget" "hpldn" {
  compartment_id = var.tenancy_ocid
  display_name   = "hpldn"
  description    = "HPLDN runs on Always Free resources: any spend is a mistake"
  amount         = var.budget_amount
  reset_period   = "MONTHLY"
  target_type    = "COMPARTMENT"
  targets        = [local.compartment_id]
  freeform_tags  = local.tags
}

resource "oci_budget_alert_rule" "actual" {
  budget_id      = oci_budget_budget.hpldn.id
  display_name   = "hpldn-actual"
  description    = "Any actual spend"
  type           = "ACTUAL"
  threshold_type = "ABSOLUTE"
  threshold      = var.budget_actual_alert
  recipients     = var.alert_email
  message        = "HPLDN: actual spend in compartment ${var.compartment_name} has reached ${var.budget_actual_alert}. Everything there should be Always Free."
}

resource "oci_budget_alert_rule" "forecast" {
  budget_id      = oci_budget_budget.hpldn.id
  display_name   = "hpldn-forecast"
  description    = "Forecast over the budget"
  type           = "FORECAST"
  threshold_type = "ABSOLUTE"
  threshold      = var.budget_amount
  recipients     = var.alert_email
  message        = "HPLDN: forecast spend in compartment ${var.compartment_name} is over ${var.budget_amount} this month."
}

# ---------------------------------------------------------------------------
# Quotas: in compartment hpldn nothing but the Always Free A1 shape (4 OCPU,
# 24 GB), 200 GB of block storage, no load balancer and no database can
# exist. Paid resources cannot be created, whatever the deployer runs.
# ---------------------------------------------------------------------------
resource "oci_limits_quota" "hpldn" {
  compartment_id = var.tenancy_ocid
  name           = "hpldn-free-tier-only"
  description    = "Compartment hpldn: Always Free A1 and 200 GB block storage only"
  statements = [
    "zero compute-core quotas in compartment ${var.compartment_name}",
    "set compute-core quota standard-a1-core-count to 4 in compartment ${var.compartment_name}",
    "set compute-core quota standard-a1-core-regional-count to 4 in compartment ${var.compartment_name}",
    "zero compute-memory quotas in compartment ${var.compartment_name}",
    "set compute-memory quota standard-a1-memory-count to 24 in compartment ${var.compartment_name}",
    "set compute-memory quota standard-a1-memory-regional-count to 24 in compartment ${var.compartment_name}",
    "set block-storage quota total-storage-gb to 200 in compartment ${var.compartment_name}",
    "zero load-balancer quotas in compartment ${var.compartment_name}",
    "zero database quotas in compartment ${var.compartment_name}",
  ]
  freeform_tags = local.tags

  depends_on = [oci_identity_compartment.hpldn]
}
