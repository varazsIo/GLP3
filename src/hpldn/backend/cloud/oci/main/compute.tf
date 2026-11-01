data "oci_identity_availability_domains" "all" {
  compartment_id = var.tenancy_ocid
}

# Canonical Ubuntu 24.04 for the A1 shape (aarch64, not the Minimal image),
# newest first. A newer image never replaces the host (ignore_changes below):
# unattended-upgrades keeps the running one patched.
data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_id
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
  state                    = "AVAILABLE"

  filter {
    name   = "display_name"
    values = ["^Canonical-Ubuntu-24\\.04-aarch64-.*"]
    regex  = true
  }
}

locals {
  availability_domain = data.oci_identity_availability_domains.all.availability_domains[var.ad_index % length(data.oci_identity_availability_domains.all.availability_domains)].name
  image_id            = data.oci_core_images.ubuntu.images[0].id

  # The tailnet key is the only secret in user_data: single-use,
  # pre-authorised, an hour long, and consumed at first boot.
  cloud_init = templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
    tailnet_key       = tailscale_tailnet_key.host.key
    ts_hostname       = "hpldn"
    domain            = var.domain
    tailscale_key_fpr = "2596A99EAAB33821893C0A79458CA832957F5868" # gitleaks:allow (public fingerprint or test fixture)
  })
}

resource "oci_core_instance" "hpldn" {
  compartment_id      = var.compartment_id
  availability_domain = local.availability_domain
  display_name        = "hpldn"
  shape               = "VM.Standard.A1.Flex"
  freeform_tags       = local.tags

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gb
  }

  source_details {
    source_type             = "image"
    source_id               = local.image_id
    boot_volume_size_in_gbs = tostring(var.boot_gb)
    # The boot volume is encrypted with the customer-managed Vault key.
    kms_key_id = var.kms_key_id
  }

  # Data between the instance and its boot volume is encrypted in transit.
  is_pv_encryption_in_transit_enabled = true

  # IMDSv2 only: the v1 endpoints (no header needed, so any SSRF reaches
  # them) are off. The host firewall keeps v2 to root as well.
  instance_options {
    are_legacy_imds_endpoints_disabled = true
  }

  # No OS management agent and no Run Command (a path to run commands on the
  # host from the console or API); monitoring stays on.
  agent_config {
    is_management_disabled = true
    is_monitoring_disabled = false

    plugins_config {
      name          = "Compute Instance Run Command"
      desired_state = "DISABLED"
    }

    plugins_config {
      name          = "Bastion"
      desired_state = "DISABLED"
    }
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.public.id
    assign_public_ip = "true"
    hostname_label   = "hpldn"
    nsg_ids          = [oci_core_network_security_group.host.id]
  }

  metadata = {
    ssh_authorized_keys = trimspace(var.ssh_public_key)
    user_data           = base64encode(local.cloud_init)
  }

  # Shielded instances (secure/measured boot) are not offered on A1.
  preserve_boot_volume = false

  lifecycle {
    # A new image, or a new (single-use) tailnet key in user_data once the
    # first one is consumed, must never replace the running host.
    ignore_changes = [source_details[0].source_id, metadata["user_data"]]
  }
}
