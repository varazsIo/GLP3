# The host's one-time tailnet key, in cloud-init's user_data. Single use,
# pre-authorised (no admin approval round trip), tagged tag:hpldn (so the
# node is owned by the tag, not by a person, and gets only what the ACL gives
# that tag: nothing outbound), and an hour long. The tailnet policy itself
# (README.md) is not managed here, so OpenTofu can never clobber it.
resource "tailscale_tailnet_key" "host" {
  reusable      = false
  ephemeral     = false
  preauthorized = true
  expiry        = 3600
  tags          = ["tag:hpldn"]
  description   = "hpldn host first boot"
}
