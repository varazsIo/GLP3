output "public_ip" {
  description = "The origin's address (only Cloudflare, with the origin-pull certificate, gets an answer on it)."
  value       = oci_core_instance.hpldn.public_ip
}

output "instance_id" {
  value = oci_core_instance.hpldn.id
}

output "tailnet_hostname" {
  value = "hpldn"
}

output "domain" {
  value = var.domain
}

output "availability_domain" {
  value = oci_core_instance.hpldn.availability_domain
}

output "image_id" {
  value = local.image_id
}

output "origin_cert_expires_on" {
  value = cloudflare_origin_ca_certificate.origin.expires_on
}

output "guard_token_expires_on" {
  value = cloudflare_account_token.guard.expires_on
}

# --- Sensitive: read by provision.py in memory and pushed to the host on SSH
# stdin (bootstrap.sh --set-tls / --set-cloudflare-token); never printed.

output "origin_cert_pem" {
  value     = cloudflare_origin_ca_certificate.origin.certificate
  sensitive = true
}

output "origin_key_pem" {
  value     = tls_private_key.origin.private_key_pem
  sensitive = true
}

output "aop_ca_pem" {
  description = "Public, but kept with its siblings: nginx's ssl_client_certificate."
  value       = tls_self_signed_cert.aop_ca.cert_pem
  sensitive   = true
}

output "guard_token" {
  value     = cloudflare_account_token.guard.value
  sensitive = true
}

output "cloudflare_account_id" {
  value     = var.cloudflare_account_id
  sensitive = true
}
