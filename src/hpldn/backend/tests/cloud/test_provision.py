"""
`cloud.oci.provision`: secrets never on argv, never in a child's environment unasked, never printed.

Hermetic: no tofu, OCI, Cloudflare, Tailscale or host is touched.
"""

import contextlib
import io
import json
import subprocess
import sys
import tarfile
import types

import pytest

from hpldn.backend.cloud.oci import provision
from hpldn.backend.common.secrets import register_secret

PASSPHRASE = "a passphrase long enough to derive a key from"


def test_tf_log_and_cli_arg_injection_are_refused():
    for name in ("TF_LOG", "TF_LOG_PROVIDER", "TF_CLI_ARGS", "TF_CLI_ARGS_apply"):
        with pytest.raises(provision.ProvisionError, match=name):
            provision.refuse_logging_env({name: "TRACE", "PATH": "/usr/bin"})
    provision.refuse_logging_env({"PATH": "/usr/bin"})


def test_a_child_gets_the_allowlist_and_only_the_secrets_named_for_it():
    ours = {"PATH": "/usr/bin", "HOME": "/home/op", "CLOUDFLARE_API_TOKEN": "cf", "HPLDN_TOFU_PASSPHRASE": PASSPHRASE,
            "AWS_SECRET_ACCESS_KEY": "aws", "RANDOM_THING": "x"}
    env = provision.child_env({"TF_VAR_state_passphrase": PASSPHRASE}, environ=ours)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/op"
    assert env["TF_VAR_state_passphrase"] == PASSPHRASE
    for leaked in ("CLOUDFLARE_API_TOKEN", "HPLDN_TOFU_PASSPHRASE", "AWS_SECRET_ACCESS_KEY", "RANDOM_THING"):
        assert leaked not in env


def test_a_secret_on_a_command_line_is_refused():
    register_secret("argv-secret-value-0001")
    with pytest.raises(provision.ProvisionError, match="command line"):
        provision.run(["echo", "--token=argv-secret-value-0001"])


def test_child_output_is_redacted(capfd):
    register_secret("printed-secret-value-0002")
    env = provision.child_env({"LEAKY": "printed-secret-value-0002"})
    provision.run([sys.executable, "-c", "import os; print('got', os.environ['LEAKY'])"], env=env)
    err = capfd.readouterr().err
    assert "got ***" in err and "printed-secret-value-0002" not in err


def test_captured_output_is_returned_for_parsing_but_not_printed(capfd):
    register_secret("captured-secret-value-0003")
    env = provision.child_env({"VALUE": "captured-secret-value-0003"})
    done = provision.run([sys.executable, "-c", "import os, sys; print(os.environ['VALUE']); print(os.environ['VALUE'], file=sys.stderr)"],
                         env=env, capture=True)
    assert done.stdout.strip() == "captured-secret-value-0003"
    assert "captured-secret-value-0003" not in capfd.readouterr().err


def test_a_failure_message_is_redacted():
    register_secret("failing-secret-value-0004")
    env = provision.child_env({"VALUE": "failing-secret-value-0004"})
    with pytest.raises(provision.ProvisionError) as raised:
        provision.run([sys.executable, "-c", "import os, sys; print(os.environ['VALUE']); sys.exit(3)"], env=env, echo=False)
    assert "failing-secret-value-0004" not in str(raised.value)


def test_secrets_come_from_the_environment_and_a_short_passphrase_is_refused():
    with pytest.raises(provision.ProvisionError, match="at least 32"):
        provision.Secrets.from_env({"HPLDN_TOFU_PASSPHRASE": "short"}, need_cloud=False)
    with pytest.raises(provision.ProvisionError, match="CLOUDFLARE_API_TOKEN"):
        provision.Secrets.from_env({"HPLDN_TOFU_PASSPHRASE": PASSPHRASE}, need_cloud=True)
    found = provision.Secrets.from_env(
        {"HPLDN_TOFU_PASSPHRASE": PASSPHRASE, "CLOUDFLARE_API_TOKEN": "cf-token-for-the-test-000",
         "TAILSCALE_OAUTH_CLIENT_ID": "kclient", "TAILSCALE_OAUTH_CLIENT_SECRET": "tskey-client-secret-000"},
        need_cloud=True,
    )
    assert provision.redact(f"{found.cloudflare_token} {found.passphrase}") == "*** ***"


def test_the_backend_config_holds_no_credentials():
    state = {"s3_endpoint": "https://ns.compat.objectstorage.uk-london-1.oraclecloud.com", "s3_access_key_id": "AKID", "s3_secret_key": "SECRET"}
    settings = provision.Settings(region="uk-london-1", tenancy_ocid="t", domain="d", zone_name="z", cloudflare_account_id="a",
                                  alert_email="e", ssh_public_key_file=provision.Path("/x"), deployer_public_key_file=provision.Path("/y"))
    text = provision.backend_config(state, settings)
    assert 'endpoints = { s3 = "https://ns.compat' in text and "use_lockfile = false" in text
    assert "AKID" not in text and "SECRET" not in text


def test_host_keys_are_read_from_between_the_console_markers():
    console = "\n".join([
        "[   10.1] cloud-init[1]: something",
        "ssh-ed25519 AAAAoutsideTheMarkersIsIgnored",
        "HPLDN-HOST-KEYS-BEGIN",
        "HPLDN-HOST-KEY ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKey1",
        "HPLDN-HOST-FP 256 SHA256:abc (ED25519)",
        "[ 12.0] HPLDN-HOST-KEY ecdsa-sha2-nistp256 AAAAE2VjZHNhHostKey2=",
        "HPLDN-HOST-KEY ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKey1",
        "HPLDN-HOST-KEYS-END",
        "HPLDN-HOST-KEY ssh-rsa AAAAafterTheEnd",
    ])
    keys = provision.parse_host_keys(console)
    assert keys == [("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIHostKey1"), ("ecdsa-sha2-nistp256", "AAAAE2VjZHNhHostKey2=")]
    lines = provision.known_hosts_lines(keys, ["hpldn", "203.0.113.10"])
    assert lines.splitlines()[0] == "hpldn,203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKey1"
    with pytest.raises(provision.ProvisionError):
        provision.known_hosts_lines([], ["hpldn"])


def test_ssh_trusts_only_the_pinned_keys():
    options = provision.ssh_options()
    assert "StrictHostKeyChecking=yes" in options
    assert any(o.startswith("UserKnownHostsFile=") and o.endswith("/.config/hpldn/known_hosts") for o in options)
    assert "StrictHostKeyChecking=accept-new" not in options



def test_a_tagged_or_misnamed_laptop_is_refused_before_ssh():
    running = {"BackendState": "Running"}
    assert provision.tailnet_client_problem({**running, "Self": {"HostName": "laptop", "Tags": None}}) is None
    tagged = provision.tailnet_client_problem({**running, "Self": {"HostName": "laptop", "Tags": ["tag:hpldn"]}})
    assert "tagged node" in tagged and "--force-reauth" in tagged
    assert "hostname" in provision.tailnet_client_problem({**running, "Self": {"HostName": "hpldn"}})
    assert "Stopped" in provision.tailnet_client_problem({"BackendState": "Stopped", "Self": {}})

class _FakeTofu:
    def __init__(self, calls, outputs, **overrides):
        self.calls, self._outputs, self.overrides = calls, outputs, overrides

    def outputs(self):
        return dict(self._outputs)


def _capacity_harness(monkeypatch, outputs, results):
    calls, applied = [], []
    monkeypatch.setattr(provision, "_availability_domains", lambda settings, state: ["AD-1", "AD-2", "AD-3"])
    monkeypatch.setattr(provision, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))

    def apply_main(tofu, tmp, *extra):
        applied.append(tofu.overrides.get("ad_index"))
        return subprocess.CompletedProcess([], *results.pop(0))

    monkeypatch.setattr(provision, "apply_main", apply_main)
    return (lambda **overrides: _FakeTofu(calls, outputs, **overrides)), applied


def test_an_existing_instance_is_applied_only_in_its_own_availability_domain(monkeypatch, tmp_path):
    tofu_for, applied = _capacity_harness(monkeypatch, {"instance_id": "ocid1.instance.x", "availability_domain": "AD-2"}, [(0, "")])
    provision.apply_with_capacity_retry(tofu_for, None, {}, tmp_path)
    assert applied == [1]

    tofu_for, applied = _capacity_harness(monkeypatch, {"instance_id": "ocid1.instance.x", "availability_domain": "AD-2"},
                                          [(1, "Out of host capacity")])
    with pytest.raises(provision.ProvisionError, match="tofu apply failed"):
        provision.apply_with_capacity_retry(tofu_for, None, {}, tmp_path)
    assert applied == [1]


def test_a_new_instance_moves_to_the_next_domain_on_out_of_capacity(monkeypatch, tmp_path):
    tofu_for, applied = _capacity_harness(monkeypatch, {}, [(1, "Out of host capacity"), (0, "")])
    provision.apply_with_capacity_retry(tofu_for, None, {}, tmp_path)
    assert applied == [0, 1]


def test_bootstrap_resumes_on_the_existing_instance_without_apply(monkeypatch, tmp_path):
    steps = []
    known = tmp_path / "known_hosts"
    known.write_text("hpldn ssh-ed25519 AAAA\n")
    out = {"instance_id": "ocid1.instance.x", "public_ip": "192.0.2.1"}
    monkeypatch.setattr(provision, "KNOWN_HOSTS", known)
    monkeypatch.setattr(provision.Secrets, "from_env", classmethod(lambda cls, **kw: "secrets"))
    monkeypatch.setattr(provision, "_main_with_state", lambda s, sec: ({}, lambda **o: _FakeTofu(steps, out, **o)))
    monkeypatch.setattr(provision, "operator_lock", contextlib.nullcontext)
    monkeypatch.setattr(provision, "init_main", lambda *a: steps.append("init"))
    monkeypatch.setattr(provision, "apply_main", lambda *a: pytest.fail("bootstrap must not apply"))
    monkeypatch.setattr(provision, "pin_host_keys", lambda *a: pytest.fail("keys are already pinned"))
    monkeypatch.setattr(provision, "wait_for_tailnet", lambda: steps.append("wait"))
    monkeypatch.setattr(provision, "deploy_sh", lambda args: steps.append(("deploy.sh", *args)))
    monkeypatch.setattr(provision, "push_secrets", lambda o, sec: steps.append("secrets"))
    monkeypatch.setattr(provision, "ssh", lambda argv, **kw: steps.append(argv[-1]))
    monkeypatch.setattr(provision, "smoke_test", lambda domain, ip: steps.append(("smoke", ip)))
    monkeypatch.setattr(provision, "refuse_stray_origin_records", lambda s, sec, ip: steps.append(("records", ip)))
    settings = types.SimpleNamespace(domain="hpldn.example.co.uk", bootstrap_ssh=False, model=None)

    args = provision.build_parser().parse_args(["bootstrap"])
    assert provision.cmd_bootstrap(settings, args) == 0
    assert steps == ["init", ("records", "192.0.2.1"), "wait", ("deploy.sh", "--bootstrap", "--domain", "hpldn.example.co.uk"),
                     "secrets", "--close-public-ssh"]

    steps.clear()
    args = provision.build_parser().parse_args(["bootstrap", "--dump", "tmp/serving/d"])
    assert provision.cmd_bootstrap(settings, args) == 0
    assert steps[-2:] == [("deploy.sh", "--dump", "tmp/serving/d"), ("smoke", "192.0.2.1")]


def test_the_tls_bundle_holds_exactly_the_three_files_bootstrap_wants():
    blob = provision.tls_bundle("CERT\n", "KEY\n", "CA\n")
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        members = tar.getmembers()
        assert [m.name for m in members] == ["origin.crt", "origin.key", "aop_ca.pem"]
        assert all(m.isfile() and m.mode == 0o600 and m.uid == 0 for m in members)
        assert tar.extractfile("origin.key").read() == b"KEY\n"


def test_the_leak_scan_sends_hash_prefixes_never_values():
    hashes = provision.leak_hashes(["the-guard-token-value", "", "recaptcha-secret"])
    assert len(hashes) == 2 and all(len(h) == 16 and int(h, 16) >= 0 for h in hashes)
    assert "the-guard-token-value" not in json.dumps(hashes)


def test_the_leak_scan_script_finds_a_secret_by_its_hash(tmp_path, monkeypatch):
    """LEAK_SCAN as the host runs it, against a fake journal."""
    import subprocess

    journal = tmp_path / "journalctl"
    journal.write_text("#!/bin/sh\necho 'worker started'\necho 'Authorization: Bearer the-guard-token-value'\n")
    journal.chmod(0o755)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin"}
    hashes = json.dumps(provision.leak_hashes(["the-guard-token-value"]))
    found = subprocess.run([sys.executable, "-I", "-c", provision.LEAK_SCAN], input=hashes, capture_output=True, text=True, env=env)
    assert found.returncode == 1 and "LEAK journal: 1" in found.stdout
    clean = subprocess.run([sys.executable, "-I", "-c", provision.LEAK_SCAN], input=json.dumps(provision.leak_hashes(["other"])),
                           capture_output=True, text=True, env=env)
    assert clean.returncode == 0


def test_tf_vars_carry_settings_not_secrets():
    env = provision.tf_vars({"domain": "hpldn.example.co.uk", "bootstrap_ssh": False, "ocpus": 4})
    assert env == {"TF_VAR_domain": "hpldn.example.co.uk", "TF_VAR_bootstrap_ssh": "false", "TF_VAR_ocpus": "4"}


def test_the_settings_file_is_read(tmp_path):
    path = tmp_path / "oci.toml"
    path.write_text((provision.OCI_DIR / "oci.example.toml").read_text())
    settings = provision.Settings.load(path)
    assert settings.domain == "hpldn.example.co.uk" and settings.bootstrap_ssh is False and settings.use_lockfile is False
    path.write_text("[oci]\nregion = 'uk-london-1'\n")
    with pytest.raises(provision.ProvisionError, match="missing"):
        provision.Settings.load(path)


def _cloudflare(monkeypatch, handler):
    """httpx.Client with `handler` behind it, whatever base_url/headers the code passes."""
    import httpx

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return httpx


def test_the_rulesets_to_adopt_are_looked_up_and_an_unreadable_redirect_phase_only_warns(monkeypatch):
    def handler(request):
        if request.url.path.endswith("/zones"):
            return httpx.Response(200, json={"result": [{"id": "z1"}]})
        if "http_request_firewall_custom" in request.url.path:
            return httpx.Response(200, json={"result": {"id": "fw1"}})
        return httpx.Response(403, json={"errors": [{"message": "request is not authorized"}]})

    httpx = _cloudflare(monkeypatch, handler)
    said = []
    monkeypatch.setattr(provision, "say", said.append)
    settings = types.SimpleNamespace(zone_name="example.co.uk")
    secrets = types.SimpleNamespace(cloudflare_token="cf")
    assert provision.entrypoint_ids(settings, secrets) == {
        "firewall_entrypoint_id": "fw1", "redirect_entrypoint_id": "", "cache_entrypoint_id": "",
    }
    assert any("Single Redirect" in line for line in said)
    assert any("Cache Rules" in line for line in said)


def test_the_smoke_test_names_a_placeholder_origin_certificate(monkeypatch):
    import httpx

    _cloudflare(monkeypatch, lambda request: httpx.Response(526))
    monkeypatch.setattr(provision, "say", lambda line: None)
    with pytest.raises(provision.ProvisionError, match="526.*placeholder"):
        provision.smoke_test("hpldn.example.co.uk", "192.0.2.1")


def test_the_smoke_test_refuses_a_page_cloudflare_injects_a_script_into(monkeypatch):
    import httpx

    page = "<html><head><script defer src=\"/static/js/main.js\"></script></head><body><div id=\"root\"></div>"
    injected = page + "<script>(function(){window.__CF$cv$params={r:'a4c7'};a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'})();</script>"

    served = {"/": injected}

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text=served["/"])
        if request.url.path == "/api/areas":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    monkeypatch.setattr(provision, "say", lambda line: None)
    _cloudflare(monkeypatch, handler)
    with pytest.raises(provision.ProvisionError, match="injects a script.*Bot Fight Mode"):
        provision.smoke_test("hpldn.example.co.uk", "192.0.2.1")

    served["/"] = page
    monkeypatch.setattr(provision.socket, "create_connection", lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError()))
    provision.smoke_test("hpldn.example.co.uk", "192.0.2.1")


def _zone_records(records):
    """A Cloudflare handler: one zone, and `records` filtered by the `name` asked for."""
    import httpx

    def handler(request):
        if request.url.path.endswith("/zones"):
            return httpx.Response(200, json={"result": [{"id": "z1"}]})
        if request.url.path == "/client/v4/zones/z1/dns_records":
            name = request.url.params["name"]
            return httpx.Response(200, json={"result": [r for r in records if r["name"] == name]})
        return httpx.Response(404)

    return handler


def test_a_second_origin_address_for_the_site_stops_up(monkeypatch):
    records = [
        {"id": "r1", "type": "A", "name": "example.co.uk", "content": "192.0.2.1"},
        {"id": "r2", "type": "A", "name": "example.co.uk", "content": "198.51.100.7"},
        {"id": "r3", "type": "CNAME", "name": "www.example.co.uk", "content": "example.co.uk"},
        {"id": "r4", "type": "AAAA", "name": "www.example.co.uk", "content": "2001:db8::1"},
        {"id": "r5", "type": "A", "name": "mail.example.co.uk", "content": "203.0.113.9"},
    ]
    _cloudflare(monkeypatch, _zone_records(records))
    settings = types.SimpleNamespace(zone_name="example.co.uk", domain="example.co.uk")
    secrets = types.SimpleNamespace(cloudflare_token="cf")
    assert [r["id"] for r in provision.stray_origin_records(settings, secrets, "192.0.2.1")] == ["r2", "r4"]
    with pytest.raises(provision.ProvisionError, match=r"198\.51\.100\.7 \(id r2\).*2001:db8::1 \(id r4\).*delete"):
        provision.refuse_stray_origin_records(settings, secrets, "192.0.2.1")


def test_only_the_instance_address_passes(monkeypatch):
    records = [{"id": "r1", "type": "A", "name": "example.co.uk", "content": "192.0.2.1"},
               {"id": "r3", "type": "CNAME", "name": "www.example.co.uk", "content": "example.co.uk"}]
    _cloudflare(monkeypatch, _zone_records(records))
    settings = types.SimpleNamespace(zone_name="example.co.uk", domain="example.co.uk")
    provision.refuse_stray_origin_records(settings, types.SimpleNamespace(cloudflare_token="cf"), "192.0.2.1")


def test_the_smoke_test_refuses_a_page_slow_through_cloudflare(monkeypatch):
    import httpx

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        if request.url.path == "/api/areas":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    monkeypatch.setattr(provision, "say", lambda line: None)
    _cloudflare(monkeypatch, handler)
    monkeypatch.setattr(provision, "ORIGIN_TTFB_LIMIT_S", -1.0)
    with pytest.raises(provision.ProvisionError, match="to answer through Cloudflare.*stray origin record"):
        provision.smoke_test("hpldn.example.co.uk", "192.0.2.1")
