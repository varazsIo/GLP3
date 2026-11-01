"""
Bot protection and abuse guard rails.

`config` reads ``guard.yaml``; `captcha` checks reCAPTCHA v3 tokens and the
signed pass the API hands out for one; the rest is the ``hpldn-guard`` daemon
(``python -m hpldn.backend.guard``), which reads nginx's guard log, bans
abusive clients and mirrors the bans into nginx and Cloudflare.
"""
