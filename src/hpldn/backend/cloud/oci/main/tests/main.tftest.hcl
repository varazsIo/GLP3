# cloud/oci/main with OCI, Cloudflare and Tailscale mocked: nothing leaves
# the laptop and no credentials are needed (`tofu test` in cloud/oci/main;
# run_all.sh lint). The tls provider is real, so the CA chain is checked for
# real.

mock_provider "oci" {
  mock_resource "oci_core_instance" {
    defaults = {
      public_ip = "203.0.113.10"
    }
  }
}

mock_provider "cloudflare" {
  mock_resource "cloudflare_account_token" {
    defaults = {
      value = "test-guard-token-value-0123456789"
    }
  }
  mock_resource "cloudflare_origin_ca_certificate" {
    defaults = {
      certificate = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n"
      expires_on  = "2027-10-10T00:00:00Z"
    }
  }
}

mock_provider "tailscale" {
  mock_resource "tailscale_tailnet_key" {
    defaults = {
      key = "tskey-auth-kTESTCNTRL-0123456789abcdef"
    }
  }
}

override_data {
  target = data.cloudflare_ip_ranges.main
  values = {
    ipv4_cidrs = ["173.245.48.0/20", "103.21.244.0/22", "104.16.0.0/13"]
    ipv6_cidrs = ["2400:cb00::/32"]
  }
}

override_data {
  target = data.cloudflare_zone.main
  values = {
    zone_id = "0123456789abcdef0123456789abcdef"
  }
}

override_data {
  target = data.cloudflare_account_api_token_permission_groups_list.filter_lists_edit
  values = {
    result = [{ id = "2edbf20661fd4661b0fe10e9e12f485c", name = "Account Rule Lists Write", category = "", is_selectable = true, scopes = ["com.cloudflare.api.account"] }]
  }
}

override_data {
  target = data.oci_core_images.ubuntu
  values = {
    images = [{ id = "ocid1.image.oc1..ubuntu2404", display_name = "Canonical-Ubuntu-24.04-aarch64-2026.09.30-0", agent_features = null, base_image_id = null, billable_size_in_gbs = null, compartment_id = null, create_image_allowed = null, defined_tags = null, freeform_tags = null, image_source_details = null, instance_id = null, launch_mode = null, launch_options = null, listing_type = null, operating_system = null, operating_system_version = null, size_in_mbs = null, state = null, time_created = null }]
  }
}

override_data {
  target = data.oci_identity_availability_domains.all
  values = {
    availability_domains = [
      { name = "Xyz:UK-LONDON-1-AD-1", id = "ad1", compartment_id = "ocid1.tenancy.oc1..aaaatest" },
      { name = "Xyz:UK-LONDON-1-AD-2", id = "ad2", compartment_id = "ocid1.tenancy.oc1..aaaatest" },
    ]
  }
}

variables {
  state_passphrase      = "correct horse battery staple, at length"
  region                = "uk-london-1"
  tenancy_ocid          = "ocid1.tenancy.oc1..aaaatest"
  compartment_id        = "ocid1.compartment.oc1..hpldn"
  kms_key_id            = "ocid1.key.oc1..cmk"
  domain                = "hpldn.example.co.uk"
  zone_name             = "example.co.uk"
  cloudflare_account_id = "0123456789abcdef0123456789abcdef"
  ssh_public_key        = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey operator"
}

run "no_ssh_ingress_and_https_from_cloudflare_only" {
  assert {
    condition     = length(oci_core_network_security_group_security_rule.ssh_break_glass) == 0
    error_message = "Nothing may listen on 22 without bootstrap_ssh."
  }

  assert {
    condition     = toset([for r in oci_core_network_security_group_security_rule.https_from_cloudflare : r.source]) == toset(["173.245.48.0/20", "103.21.244.0/22", "104.16.0.0/13"])
    error_message = "443 must be admitted from Cloudflare's ranges, and only them."
  }

  assert {
    condition     = alltrue([for r in oci_core_network_security_group_security_rule.https_from_cloudflare : r.direction == "INGRESS" && r.protocol == "6" && r.tcp_options[0].destination_port_range[0].min == 443 && r.tcp_options[0].destination_port_range[0].max == 443])
    error_message = "The Cloudflare rules open tcp/443 and nothing else."
  }

  assert {
    condition     = oci_core_network_security_group_security_rule.tailscale_direct[0].protocol == "17" && oci_core_network_security_group_security_rule.path_mtu.protocol == "1"
    error_message = "The only other ingress is Tailscale's UDP and ICMP path-MTU."
  }

  assert {
    condition     = oci_core_network_security_group_security_rule.path_mtu.icmp_options[0].type == 3 && oci_core_network_security_group_security_rule.path_mtu.icmp_options[0].code == 4
    error_message = "ICMP is type 3 code 4 only."
  }
}

run "the_default_security_list_has_no_rules" {
  assert {
    condition     = length(oci_core_default_security_list.none.ingress_security_rules) == 0 && length(oci_core_default_security_list.none.egress_security_rules) == 0
    error_message = "The default security list (which opens 22 to the world by default) must have no rules."
  }

  assert {
    condition     = oci_core_subnet.public.security_list_ids == toset([oci_core_default_security_list.none.id])
    error_message = "The subnet uses only the empty default security list."
  }
}

run "egress_is_limited" {
  assert {
    condition     = toset(keys(oci_core_network_security_group_security_rule.egress)) == toset(["https", "http", "tailscale", "stun", "dns_udp", "dns_tcp", "ntp"])
    error_message = "Egress is HTTPS, HTTP, Tailscale, STUN, DNS and NTP only."
  }

  assert {
    condition     = alltrue([for k in ["dns_udp", "dns_tcp", "ntp"] : oci_core_network_security_group_security_rule.egress[k].destination == "169.254.169.254/32"])
    error_message = "DNS and NTP go to OCI's resolver only."
  }
}

run "authenticated_origin_pulls_use_our_ca" {
  assert {
    condition     = cloudflare_authenticated_origin_pulls_settings.zone.enabled == true
    error_message = "Authenticated Origin Pulls must be on."
  }

  assert {
    condition     = cloudflare_authenticated_origin_pulls_certificate.zone.certificate == tls_locally_signed_cert.aop_client.cert_pem
    error_message = "The zone presents the leaf issued here."
  }

  assert {
    condition     = tls_locally_signed_cert.aop_client.ca_cert_pem == tls_self_signed_cert.aop_ca.cert_pem && tls_self_signed_cert.aop_ca.is_ca_certificate
    error_message = "The leaf is issued by our own CA, which is the one nginx trusts (output aop_ca_pem)."
  }

  assert {
    condition     = contains(tls_locally_signed_cert.aop_client.allowed_uses, "client_auth") && !contains(tls_locally_signed_cert.aop_client.allowed_uses, "cert_signing")
    error_message = "The leaf is a client certificate, not a CA."
  }

  assert {
    condition     = tls_private_key.origin.algorithm == "ECDSA" && tls_private_key.origin.ecdsa_curve == "P256" && cloudflare_origin_ca_certificate.origin.request_type == "origin-ecc"
    error_message = "The origin certificate is ECDSA P-256."
  }
}

run "the_site_is_proxied_and_the_zone_strict" {
  assert {
    condition     = cloudflare_dns_record.site.proxied == true && cloudflare_dns_record.site.content == "203.0.113.10"
    error_message = "The A record is proxied and points at the instance."
  }

  assert {
    condition     = cloudflare_zone_setting.basic["ssl"].value == "strict" && cloudflare_zone_setting.basic["min_tls_version"].value == "1.2"
    error_message = "Full (strict) TLS to the origin, TLS 1.2 and up."
  }

  assert {
    condition     = cloudflare_zone_setting.basic["http3"].value == "off"
    error_message = "HTTP/3 is off: iOS Safari's requests over it stalled at the edge."
  }

  assert {
    condition     = cloudflare_bot_management.main.fight_mode == false && cloudflare_bot_management.main.enable_js == false
    error_message = "Bot Fight Mode is off: it tarpitted fresh iOS Safari visitors and cannot be exempted."
  }

  assert {
    condition     = strcontains(cloudflare_ruleset.firewall.rules[0].expression, "ip.src in $hpldn_banned")
    error_message = "The banned list is blocked at the edge."
  }
}

run "www_is_redirected_to_the_apex_at_the_edge" {
  assert {
    condition     = cloudflare_ruleset.redirect.phase == "http_request_dynamic_redirect" && cloudflare_ruleset.redirect.rules[0].expression == "http.host eq \"www.hpldn.example.co.uk\""
    error_message = "www.<domain> must be matched in the zone's redirect phase."
  }

  assert {
    condition     = cloudflare_ruleset.redirect.rules[0].action_parameters.from_value.status_code == 301 && cloudflare_ruleset.redirect.rules[0].action_parameters.from_value.preserve_query_string && cloudflare_ruleset.redirect.rules[0].action_parameters.from_value.target_url.expression == "concat(\"https://hpldn.example.co.uk\", http.request.uri.path)"
    error_message = "www must 301 to https://<domain> with the path and query kept."
  }
}

run "the_guard_token_has_one_permission_on_this_account" {
  assert {
    condition     = length(cloudflare_account_token.guard.policies) == 1 && length(cloudflare_account_token.guard.policies[0].permission_groups) == 1
    error_message = "The guard token has exactly one policy with one permission group."
  }

  assert {
    condition     = cloudflare_account_token.guard.policies[0].permission_groups[0].id == "2edbf20661fd4661b0fe10e9e12f485c"
    error_message = "That group is Account Rule Lists Write."
  }

  assert {
    condition     = jsondecode(cloudflare_account_token.guard.policies[0].resources) == { "com.cloudflare.api.account.0123456789abcdef0123456789abcdef" = "*" }
    error_message = "The token is scoped to this account only."
  }

  assert {
    condition     = cloudflare_account_token.guard.expires_on != null
    error_message = "The token expires."
  }
}

run "user_data_holds_only_the_single_use_tailnet_key" {
  assert {
    condition = alltrue([for needle in ["PRIVATE KEY", "tskey-api", "tskey-client", "cfut_", "test-guard-token-value", var.state_passphrase, "RECAPTCHA", "BEGIN CERTIFICATE"] :
    !strcontains(base64decode(oci_core_instance.hpldn.metadata["user_data"]), needle)])
    error_message = "user_data must hold no private key, API or OAuth key, Cloudflare token, certificate, reCAPTCHA value or passphrase."
  }

  assert {
    condition     = strcontains(base64decode(oci_core_instance.hpldn.metadata["user_data"]), tailscale_tailnet_key.host.key)
    error_message = "The tailnet key is the one secret in user_data."
  }

  assert {
    condition     = tailscale_tailnet_key.host.reusable == false && tailscale_tailnet_key.host.ephemeral == false && tailscale_tailnet_key.host.preauthorized == true && tailscale_tailnet_key.host.expiry <= 3600
    error_message = "The tailnet key is single-use and at most an hour long."
  }

  assert {
    condition     = tailscale_tailnet_key.host.tags == toset(["tag:hpldn"])
    error_message = "The host joins as tag:hpldn."
  }

  assert {
    condition     = strcontains(base64decode(oci_core_instance.hpldn.metadata["user_data"]), "--auth-key=file:/run/hpldn/ts.key")
    error_message = "The tailnet key is read from a file, never passed on argv."
  }
}

run "the_instance_is_locked_down" {
  assert {
    condition     = oci_core_instance.hpldn.instance_options[0].are_legacy_imds_endpoints_disabled == true
    error_message = "IMDSv1 must be off."
  }

  assert {
    condition     = oci_core_instance.hpldn.agent_config[0].is_management_disabled == true
    error_message = "The management agent must be off."
  }

  assert {
    condition     = contains([for p in oci_core_instance.hpldn.agent_config[0].plugins_config : p.desired_state if p.name == "Compute Instance Run Command"], "DISABLED")
    error_message = "Run Command must be off."
  }

  assert {
    condition     = oci_core_instance.hpldn.source_details[0].kms_key_id == "ocid1.key.oc1..cmk" && oci_core_instance.hpldn.is_pv_encryption_in_transit_enabled == true
    error_message = "The boot volume uses the CMK and is encrypted in transit."
  }

  assert {
    condition     = oci_core_instance.hpldn.shape == "VM.Standard.A1.Flex" && oci_core_instance.hpldn.shape_config[0].ocpus == 4 && oci_core_instance.hpldn.shape_config[0].memory_in_gbs == 24
    error_message = "The Always Free A1 shape."
  }

  assert {
    condition     = oci_core_instance.hpldn.create_vnic_details[0].nsg_ids == toset([oci_core_network_security_group.host.id])
    error_message = "The VNIC is in the host NSG."
  }
}

run "every_secret_output_is_sensitive" {
  assert {
    condition = alltrue([
      issensitive(output.origin_cert_pem),
      issensitive(output.origin_key_pem),
      issensitive(output.aop_ca_pem),
      issensitive(output.guard_token),
    ])
    error_message = "The origin certificate and key, the AOP CA and the guard token must be sensitive outputs."
  }

  assert {
    condition     = !issensitive(output.public_ip) && !issensitive(output.instance_id)
    error_message = "The plain outputs stay plain."
  }
}

run "break_glass_ssh_opens_to_the_admin_address_only" {
  variables {
    bootstrap_ssh = true
    admin_cidr    = "198.51.100.7/32"
  }

  assert {
    condition     = oci_core_network_security_group_security_rule.ssh_break_glass[0].source == "198.51.100.7/32"
    error_message = "Break-glass SSH is from the admin address only."
  }
}

run "ssh_from_the_internet_is_refused" {
  command = plan

  variables {
    bootstrap_ssh = true
    admin_cidr    = "0.0.0.0/0"
  }

  expect_failures = [var.admin_cidr]
}

run "too_many_ocpus_are_refused" {
  command = plan

  variables {
    ocpus = 8
  }

  expect_failures = [var.ocpus]
}

run "too_much_memory_is_refused" {
  command = plan

  variables {
    memory_gb = 32
  }

  expect_failures = [var.memory_gb]
}

run "an_oversized_boot_volume_is_refused" {
  command = plan

  variables {
    boot_gb = 250
  }

  expect_failures = [var.boot_gb]
}

run "an_undersized_boot_volume_is_refused" {
  command = plan

  variables {
    boot_gb = 20
  }

  expect_failures = [var.boot_gb]
}
