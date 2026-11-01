output "region" {
  value = var.region
}

output "tenancy_ocid" {
  value = var.tenancy_ocid
}

output "compartment_id" {
  value = local.compartment_id
}

output "namespace" {
  value = data.oci_objectstorage_namespace.main.namespace
}

output "vault_id" {
  value = oci_kms_vault.hpldn.id
}

output "kms_key_id" {
  description = "The customer-managed key: main/ encrypts the boot volume with it."
  value       = oci_kms_key.hpldn.id
}

output "tfstate_bucket" {
  value = oci_objectstorage_bucket.buckets[local.tfstate_bucket].name
}

output "inputs_bucket" {
  value = oci_objectstorage_bucket.buckets[local.inputs_bucket].name
}

output "deployer_user_ocid" {
  description = "Goes in the deployer profile of ~/.oci/config (user=)."
  value       = oci_identity_user.deployer.id
}

output "deployer_key_fingerprint" {
  description = "Goes in the deployer profile of ~/.oci/config (fingerprint=)."
  value       = oci_identity_api_key.deployer.fingerprint
}

output "s3_access_key_id" {
  description = "The state backend's S3 access key id (AWS_ACCESS_KEY_ID for main/)."
  value       = oci_identity_customer_secret_key.tfstate.id
  sensitive   = true
}

output "s3_secret_key" {
  description = "The state backend's S3 secret (AWS_SECRET_ACCESS_KEY for main/). Only provision.py reads it, in memory."
  value       = oci_identity_customer_secret_key.tfstate.key
  sensitive   = true
}

output "s3_endpoint" {
  value = "https://${data.oci_objectstorage_namespace.main.namespace}.compat.objectstorage.${var.region}.oraclecloud.com"
}
