locals {
  tags = { project = "hpldn", managed_by = "opentofu-main" }

  # OCI's instance services address: DNS, NTP (and the metadata service,
  # which the host firewall keeps to root).
  instance_services = "169.254.169.254/32"

  cloudflare_v4 = toset(data.cloudflare_ip_ranges.main.ipv4_cidrs)

  # Egress, and nothing else: HTTPS (apt, uv, Google siteverify, Cloudflare's
  # API, Tailscale's control plane and DERP), HTTP (apt mirrors without
  # HTTPS), Tailscale's direct UDP and STUN, and DNS/NTP to OCI's resolver.
  egress_rules = {
    https     = { protocol = "6", destination = "0.0.0.0/0", min = 443, max = 443 }
    http      = { protocol = "6", destination = "0.0.0.0/0", min = 80, max = 80 }
    tailscale = { protocol = "17", destination = "0.0.0.0/0", min = 41641, max = 41641 }
    stun      = { protocol = "17", destination = "0.0.0.0/0", min = 3478, max = 3478 }
    dns_udp   = { protocol = "17", destination = local.instance_services, min = 53, max = 53 }
    dns_tcp   = { protocol = "6", destination = local.instance_services, min = 53, max = 53 }
    ntp       = { protocol = "17", destination = local.instance_services, min = 123, max = 123 }
  }
}

data "cloudflare_ip_ranges" "main" {}

resource "oci_core_vcn" "hpldn" {
  compartment_id = var.compartment_id
  cidr_blocks    = ["10.20.0.0/24"]
  display_name   = "hpldn"
  dns_label      = "hpldn"
  freeform_tags  = local.tags
}

resource "oci_core_internet_gateway" "hpldn" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.hpldn.id
  display_name   = "hpldn"
  enabled        = true
  freeform_tags  = local.tags
}

resource "oci_core_route_table" "public" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.hpldn.id
  display_name   = "hpldn-public"
  freeform_tags  = local.tags

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.hpldn.id
  }
}

# The VCN's default security list otherwise admits tcp/22 from anywhere (and
# ICMP). Managed here with no rules at all: every rule is in the NSG below.
resource "oci_core_default_security_list" "none" {
  manage_default_resource_id = oci_core_vcn.hpldn.default_security_list_id
  compartment_id             = var.compartment_id
  display_name               = "hpldn-default-no-rules"
  freeform_tags              = local.tags
}

resource "oci_core_subnet" "public" {
  compartment_id             = var.compartment_id
  vcn_id                     = oci_core_vcn.hpldn.id
  cidr_block                 = "10.20.0.0/24"
  display_name               = "hpldn-public"
  dns_label                  = "public"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_default_security_list.none.id]
  prohibit_public_ip_on_vnic = false
  freeform_tags              = local.tags
}

resource "oci_core_network_security_group" "host" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.hpldn.id
  display_name   = "hpldn-host"
  freeform_tags  = local.tags
}

# --- Ingress ----------------------------------------------------------------

# 443 from Cloudflare's published IPv4 ranges only (the host firewall admits
# the same, as a second layer; nginx then wants the origin-pull certificate).
resource "oci_core_network_security_group_security_rule" "https_from_cloudflare" {
  for_each = local.cloudflare_v4

  network_security_group_id = oci_core_network_security_group.host.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = each.value
  source_type               = "CIDR_BLOCK"
  stateless                 = false
  description               = "HTTPS from Cloudflare ${each.value}"

  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "tailscale_direct" {
  count = var.tailscale_direct ? 1 : 0

  network_security_group_id = oci_core_network_security_group.host.id
  direction                 = "INGRESS"
  protocol                  = "17"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  stateless                 = false
  description               = "Tailscale direct connections (WireGuard, authenticated by tailscaled)"

  udp_options {
    destination_port_range {
      min = 41641
      max = 41641
    }
  }
}

# ICMP type 3 code 4 ("fragmentation needed"): path-MTU discovery.
resource "oci_core_network_security_group_security_rule" "path_mtu" {
  network_security_group_id = oci_core_network_security_group.host.id
  direction                 = "INGRESS"
  protocol                  = "1"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  stateless                 = false
  description               = "Path-MTU discovery"

  icmp_options {
    type = 3
    code = 4
  }
}

# Break-glass only (default off): SSH from one admin address. Every normal
# session is SSH over the tailnet, so port 22 is never open to the internet.
resource "oci_core_network_security_group_security_rule" "ssh_break_glass" {
  count = var.bootstrap_ssh ? 1 : 0

  network_security_group_id = oci_core_network_security_group.host.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.admin_cidr
  source_type               = "CIDR_BLOCK"
  stateless                 = false
  description               = "Break-glass SSH from the admin address"

  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

# --- Egress -----------------------------------------------------------------

resource "oci_core_network_security_group_security_rule" "egress" {
  for_each = local.egress_rules

  network_security_group_id = oci_core_network_security_group.host.id
  direction                 = "EGRESS"
  protocol                  = each.value.protocol
  destination               = each.value.destination
  destination_type          = "CIDR_BLOCK"
  stateless                 = false
  description               = "Egress ${each.key}"

  dynamic "tcp_options" {
    for_each = each.value.protocol == "6" ? [1] : []
    content {
      destination_port_range {
        min = each.value.min
        max = each.value.max
      }
    }
  }

  dynamic "udp_options" {
    for_each = each.value.protocol == "17" ? [1] : []
    content {
      destination_port_range {
        min = each.value.min
        max = each.value.max
      }
    }
  }
}
