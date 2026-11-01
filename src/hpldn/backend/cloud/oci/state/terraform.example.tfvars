# For running cloud/oci/state by hand (provision.py passes these as TF_VAR_*).
# The passphrase is never in a file: export TF_VAR_state_passphrase.
oci_profile             = "DEFAULT"
tenancy_ocid            = "ocid1.tenancy.oc1..aaaa"
region                  = "uk-london-1"
alert_email             = "you@example.com"
deployer_public_key_pem = "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"
