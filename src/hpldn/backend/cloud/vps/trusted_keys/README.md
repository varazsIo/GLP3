# Serving dump signing keys

The Ed25519 public keys serving dumps must be signed with, one `<key id>.pem` each.
`python -m hpldn.backend.scripts.export_serving_db --keygen` writes the private key to
`~/.config/hpldn/serving-signing.key` (0600, never committed) and its public half here:
commit it. `bootstrap.sh` installs this directory as `/usr/local/share/hpldn/trusted_keys/`,
and `hpldn-restore-serving-db` refuses any dump not signed by one of them.
