data "cloudflare_zone" "main" {
  filter = {
    name = var.zone_name
  }
}

locals {
  zone_id = data.cloudflare_zone.main.zone_id

  # Settings on the zone: full TLS validation to the origin, HTTPS only,
  # TLS 1.2 and up, the browser integrity check and HSTS at the edge.
  # HTTP/3 is off: iOS Safari's requests over it stalled at the edge --
  # each reached nginx 10-25 s late, a search only when the
  # page's timeout cancelled it (499, rt=0.000), while desktop browsers on
  # the same origin connection were answered in milliseconds. Over HTTP/2
  # there is no such stall (a known WebKit HTTP/3 issue behind Cloudflare).
  zone_settings = {
    ssl                      = "strict"
    always_use_https         = "on"
    min_tls_version          = "1.2"
    tls_1_3                  = "on"
    http3                    = "off"
    browser_check            = "on"
    automatic_https_rewrites = "on"
  }
}

# The site: a proxied A record, so visitors only ever see Cloudflare.
resource "cloudflare_dns_record" "site" {
  zone_id = local.zone_id
  name    = var.domain
  type    = "A"
  content = oci_core_instance.hpldn.public_ip
  proxied = true
  ttl     = 1
  comment = "HPLDN origin (OpenTofu); reachable from Cloudflare with the origin-pull certificate only"
}

resource "cloudflare_zone_setting" "basic" {
  for_each = local.zone_settings

  zone_id    = local.zone_id
  setting_id = each.key
  value      = each.value
}

resource "cloudflare_zone_setting" "security_header" {
  zone_id    = local.zone_id
  setting_id = "security_header"
  value = {
    strict_transport_security = {
      enabled            = true
      max_age            = 31536000
      include_subdomains = true
      preload            = false
      nosniff            = true
    }
  }
}

# Bot Fight Mode (the free plan's bot protection) is OFF, and with it the JS
# detections it needs. It tarpitted fresh iOS Safari visitors
# (a Private tab on a carrier's shared address): the page took ~30 s and every
# search was held at the edge until the page gave up on it. Its detection is an
# inline <script> Cloudflare injects into the HTML, which the site's CSP
# (script-src without 'unsafe-inline' or a nonce) refuses, so every visitor
# failed it; and nothing can be exempted from Fight Mode (WAF skip rules have
# no effect). What protects the site instead: the reCAPTCHA v3 pass on the
# costly routes, nginx's and the API's per-client limits, hpldn-guard's bans
# (and their IP list here), the rate-limit rule, the WAF rules and the
# browser integrity check. The resource stays, so the setting is OpenTofu's.
resource "cloudflare_bot_management" "main" {
  zone_id    = local.zone_id
  enable_js  = false
  fight_mode = false
}

# ---------------------------------------------------------------------------
# Origin certificate: ECDSA P-256, issued by Cloudflare's Origin CA for the
# domain. The private key never leaves the laptop's (encrypted) state and the
# host (/etc/hpldn/tls, root 0600). Re-issue: provision.py up --renew-certs.
# ---------------------------------------------------------------------------
resource "tls_private_key" "origin" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "origin" {
  private_key_pem = tls_private_key.origin.private_key_pem
  dns_names       = [var.domain]

  subject {
    common_name = var.domain
  }
}

resource "cloudflare_origin_ca_certificate" "origin" {
  csr                = tls_cert_request.origin.cert_request_pem
  hostnames          = [var.domain]
  request_type       = "origin-ecc"
  requested_validity = var.origin_cert_days
}

# ---------------------------------------------------------------------------
# Authenticated Origin Pulls with OUR OWN CA, not Cloudflare's shared one
# (which every Cloudflare zone presents, so it proves nothing about which
# zone is calling). The zone's edge presents the client leaf below; nginx
# trusts only this CA (ssl_verify_client on, ssl_verify_depth 1).
# ---------------------------------------------------------------------------
resource "tls_private_key" "aop_ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "aop_ca" {
  private_key_pem       = tls_private_key.aop_ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 5 * 8766
  allowed_uses          = ["cert_signing", "crl_signing", "digital_signature"]

  subject {
    common_name  = "HPLDN origin-pull CA"
    organization = "HPLDN"
  }
}

resource "tls_private_key" "aop_client" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_cert_request" "aop_client" {
  private_key_pem = tls_private_key.aop_client.private_key_pem

  subject {
    common_name  = "HPLDN origin pull ${var.domain}"
    organization = "HPLDN"
  }
}

resource "tls_locally_signed_cert" "aop_client" {
  cert_request_pem      = tls_cert_request.aop_client.cert_request_pem
  ca_private_key_pem    = tls_private_key.aop_ca.private_key_pem
  ca_cert_pem           = tls_self_signed_cert.aop_ca.cert_pem
  validity_period_hours = 2 * 8766
  early_renewal_hours   = 30 * 24
  allowed_uses          = ["client_auth", "digital_signature", "key_agreement"]
}

resource "cloudflare_authenticated_origin_pulls_certificate" "zone" {
  zone_id     = local.zone_id
  certificate = tls_locally_signed_cert.aop_client.cert_pem
  private_key = tls_private_key.aop_client.private_key_pem
}

resource "cloudflare_authenticated_origin_pulls_settings" "zone" {
  zone_id = local.zone_id
  enabled = true

  depends_on = [cloudflare_authenticated_origin_pulls_certificate.zone]
}

# ---------------------------------------------------------------------------
# The guard's IP list (hpldn-guard keeps its items in sync with its bans; the
# list itself is OpenTofu's, its contents the guard's) and the WAF rules.
# ---------------------------------------------------------------------------
resource "cloudflare_list" "banned" {
  account_id  = var.cloudflare_account_id
  kind        = "ip"
  name        = "hpldn_banned"
  description = "Clients banned by hpldn-guard (items synced by the guard)"

  lifecycle {
    ignore_changes = [items]
  }
}

# A zone has one ruleset per phase. When the zone already has one (the
# dashboard's AI Crawl Control makes it), provision.py passes its id and it is
# adopted here, not created; its rule is kept below.
import {
  for_each = var.firewall_entrypoint_id == "" ? toset([]) : toset([var.firewall_entrypoint_id])
  to       = cloudflare_ruleset.firewall
  id       = "zones/${local.zone_id}/${each.value}"
}

resource "cloudflare_ruleset" "firewall" {
  zone_id     = local.zone_id
  name        = "hpldn-firewall"
  description = "HPLDN: banned clients, unexpected methods and the API's docs never reach the origin"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules = [
    {
      ref         = "hpldn_banned"
      description = "Clients hpldn-guard banned (verified bots excepted)"
      expression  = format("(ip.src in $%s and not cf.client.bot)", cloudflare_list.banned.name)
      action      = "block"
      enabled     = true
    },
    {
      ref         = "hpldn_methods"
      description = "Only GET, POST and HEAD exist on the site"
      expression  = "not http.request.method in {\"GET\" \"POST\" \"HEAD\"}"
      action      = "block"
      enabled     = true
    },
    {
      ref         = "hpldn_api_docs"
      description = "The API's docs are never served (the origin 404s them too)"
      expression  = "lower(http.request.uri.path) in {\"/api/docs\" \"/api/redoc\" \"/api/openapi.json\"} or starts_with(lower(http.request.uri.path), \"/api/docs/\")"
      action      = "block"
      enabled     = true
    },
    {
      # The dashboard's AI Crawl Control rule, kept as it was found.
      ref         = "[CF AI Audit]"
      description = "AI Crawl Control - Block AI bots by User Agent"
      expression  = "(http.request.uri.path ne \"/robots.txt\" and ((http.user_agent contains \"Applebot\") or (http.user_agent contains \"ChatGPT-User\") or (http.user_agent contains \"ClaudeBot\") or (http.user_agent contains \"GPTBot\") or (http.user_agent contains \"OAI-SearchBot\") or (http.user_agent contains \"PerplexityBot\")))"
      action      = "block"
      enabled     = true
    },
  ]
}

# www.<domain> is answered at the edge with a redirect to the apex, so it
# never reaches the origin, whose certificate and server_name are the apex
# only (nginx would refuse the handshake: Cloudflare 525). Like the firewall,
# a zone's existing ruleset for the phase is adopted (provision.py passes it).
import {
  for_each = var.redirect_entrypoint_id == "" ? toset([]) : toset([var.redirect_entrypoint_id])
  to       = cloudflare_ruleset.redirect
  id       = "zones/${local.zone_id}/${each.value}"
}

resource "cloudflare_ruleset" "redirect" {
  zone_id     = local.zone_id
  name        = "hpldn-redirect"
  description = "HPLDN: www goes to the apex"
  kind        = "zone"
  phase       = "http_request_dynamic_redirect"

  rules = [
    {
      ref         = "hpldn_www"
      description = "www.${var.domain} -> https://${var.domain}, path and query kept"
      expression  = "http.host eq \"www.${var.domain}\""
      action      = "redirect"
      enabled     = true
      action_parameters = {
        from_value = {
          status_code           = 301
          preserve_query_string = true
          target_url = {
            expression = "concat(\"https://${var.domain}\", http.request.uri.path)"
          }
        }
      }
    },
  ]
}

# The two public GETs, /api/areas and /api/inflation, are the same for every
# visitor and change monthly: Cloudflare keeps them at the edge for as long as
# the API's Cache-Control says (s-maxage=300, stale-while-revalidate; api/
# routers/market.py), so a new visitor gets them from the nearest data centre
# instead of a round trip to the origin. Everything else under /api/ is
# no-store, and Cloudflare caches no other path's HTML or JSON by default.
# Like the firewall, a zone's existing ruleset for the phase is adopted.
import {
  for_each = var.cache_entrypoint_id == "" ? toset([]) : toset([var.cache_entrypoint_id])
  to       = cloudflare_ruleset.cache
  id       = "zones/${local.zone_id}/${each.value}"
}

resource "cloudflare_ruleset" "cache" {
  zone_id     = local.zone_id
  name        = "hpldn-cache"
  description = "HPLDN: the public area and inflation figures are cached at the edge"
  kind        = "zone"
  phase       = "http_request_cache_settings"

  rules = [
    {
      ref         = "hpldn_public_figures"
      description = "GET /api/areas and /api/inflation: cache as the origin's Cache-Control says"
      expression  = "http.request.method eq \"GET\" and http.request.uri.path in {\"/api/areas\" \"/api/inflation\"}"
      action      = "set_cache_settings"
      enabled     = true
      action_parameters = {
        cache       = true
        edge_ttl    = { mode = "respect_origin" }
        browser_ttl = { mode = "respect_origin" }
      }
    },
  ]
}

# The free plan's one rate-limiting rule: per client IP (and data centre),
# 10-second period, on /api/ only. nginx and the API keep their own,
# per-route limits behind it.
resource "cloudflare_ruleset" "ratelimit" {
  zone_id     = local.zone_id
  name        = "hpldn-ratelimit"
  description = "HPLDN: per-client limit on /api/"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules = [
    {
      ref         = "hpldn_api_rate"
      description = "At most ${var.api_rate_limit} /api/ requests per client per 10 s"
      expression  = "starts_with(http.request.uri.path, \"/api/\")"
      action      = "block"
      enabled     = true
      ratelimit = {
        characteristics     = ["cf.colo.id", "ip.src"]
        period              = 10
        requests_per_period = var.api_rate_limit
        mitigation_timeout  = 10
      }
    },
  ]
}

# ---------------------------------------------------------------------------
# The guard's token: one permission group (Account Rule Lists Write) on
# this account only, a year long. Rotate: provision.py up --rotate-guard-token.
# ---------------------------------------------------------------------------
data "cloudflare_account_api_token_permission_groups_list" "filter_lists_edit" {
  account_id = var.cloudflare_account_id
  name       = "Account Rule Lists Write"
}

# The token's issue time, fixed once (a timestamp() in expires_on itself
# would change every plan).
resource "terraform_data" "guard_token_issued" {
  input = timestamp()

  lifecycle {
    ignore_changes = [input]
  }
}

locals {
  filter_lists_edit_groups = [
    for group in data.cloudflare_account_api_token_permission_groups_list.filter_lists_edit.result : group
    if group.name == "Account Rule Lists Write"
  ]
}

resource "cloudflare_account_token" "guard" {
  account_id = var.cloudflare_account_id
  name       = "hpldn-guard (${var.domain})"
  expires_on = formatdate("YYYY-MM-DD'T'hh:mm:ss'Z'", timeadd(terraform_data.guard_token_issued.output, "8760h"))

  policies = [
    {
      effect            = "allow"
      permission_groups = [for group in local.filter_lists_edit_groups : { id = group.id }]
      resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
    },
  ]

  lifecycle {
    precondition {
      condition     = length(local.filter_lists_edit_groups) == 1
      error_message = "Expected exactly one Cloudflare permission group named \"Account Rule Lists Write\"."
    }
  }
}
