# For running cloud/oci/main by hand (provision.py passes these as TF_VAR_*).
# Never in a file: TF_VAR_state_passphrase, CLOUDFLARE_API_TOKEN,
# TAILSCALE_OAUTH_CLIENT_ID/_SECRET, AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY.
oci_profile           = "hpldn-deployer"
region                = "uk-london-1"
tenancy_ocid          = "ocid1.tenancy.oc1..aaaa"
compartment_id        = "ocid1.compartment.oc1..aaaa"
kms_key_id            = "ocid1.key.oc1.uk-london-1.aaaa"
domain                = "hpldn.example.co.uk"
zone_name             = "example.co.uk"
cloudflare_account_id = "0123456789abcdef0123456789abcdef"
ssh_public_key        = "ssh-ed25519 AAAA... operator"
bootstrap_ssh         = false
