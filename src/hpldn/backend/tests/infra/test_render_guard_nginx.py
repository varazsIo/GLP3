"""
``cloud/vps/render_guard_nginx.py``: guard.yaml to nginx includes, on the system python3.

The script is loaded from its file (it is not part of the package). Its
output from the repository's guard.yaml is checked here; ``nginx -t`` over
the whole rendered site is `test_local_stack.test_nginx_accepts_the_render`.
"""

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "cloud" / "vps" / "render_guard_nginx.py"
GUARD_YAML = SCRIPT.with_name("guard.yaml")

spec = importlib.util.spec_from_file_location("render_guard_nginx", SCRIPT)
rgn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rgn)


@pytest.fixture
def config() -> dict:
    return rgn.load(GUARD_YAML)


def test_the_repository_config_renders_every_file(config):
    files = rgn.render(config)
    routes = config["rate_limits"]["nginx"]["routes"]
    assert set(files) == {"limits_http.conf", "server_limits.conf", "bad_ua.conf", *(f"limits/{r}.conf" for r in routes)}
    assert all(text.startswith(rgn.GENERATED) for text in files.values())
    http = files["limits_http.conf"]
    for name, zone in config["rate_limits"]["nginx"]["zones"].items():
        key = "$hpldn_crawler_key" if name == "crawler" else "$hpldn_limit_key"
        assert f"limit_req_zone {key} zone=hpldn_{name}:{zone.get('size', '10m')} rate={zone['rate']};" in http
    for name, route in routes.items():
        text = files[f"limits/{name}.conf"]
        assert f"limit_req zone=hpldn_{route['zone']} burst={route['burst']} nodelay;" in text
        assert ("limit_conn hpldn_search_conn" in text) == bool(route.get("search_conn"))


def test_user_agent_tokens_become_escaped_case_insensitive_patterns():
    data = {"rate_limits": {"nginx": {"zones": {"crawler": {"rate": "10r/s"}, "api": {"rate": "60r/m"}},
                                       "routes": {"api": {"location": "/api/", "zone": "api", "burst": 5}},
                                       "conn_per_client": 20, "search_conn_per_client": 4,
                                       "timeouts": {"client_header": "10s", "client_body": 10, "send": "1m", "keepalive": 30}}},
            "user_agents": {"deny_on_api": ["", "python-requests", "Go-http-client/1.1", None]}}
    files = rgn.render(data)
    ua = files["bad_ua.conf"]
    assert '    "" 1;' in ua
    assert '"~*python-requests" 1;' in ua
    assert r'"~*Go-http-client\/1\.1" 1;' in ua
    assert "send_timeout 60s;" in files["server_limits.conf"]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("rate_limits.nginx.zones.crawler", None, "crawler zone"),
        ("rate_limits.nginx.routes", {}, "routes is empty"),
        ("rate_limits.nginx.zones.api_extra", {"rate": "lots"}, "rate"),
        ("rate_limits.nginx.zones.api_extra", {"rate": "1r/s", "size": "huge"}, "size"),
        ("user_agents.deny_on_api", ["evil;rm -rf"], "may only hold"),
        ("user_agents.deny_on_api", "curl", "must be a list"),
    ],
)
def test_an_invalid_config_is_refused(config, path, value, message):
    data = copy.deepcopy(config)
    *parents, last = path.split(".")
    node = data
    for part in parents:
        node = node[part]
    if value is None:
        del node[last]
    else:
        node[last] = value
    with pytest.raises(rgn.ConfigError, match=message):
        rgn.render(data)


def test_a_route_on_an_unknown_zone_is_refused(config):
    data = copy.deepcopy(config)
    route = next(iter(data["rate_limits"]["nginx"]["routes"].values()))
    route["zone"] = "crawler"
    with pytest.raises(rgn.ConfigError, match="unknown zone"):
        rgn.render(data)


def test_write_changes_only_what_changed_and_drops_stale_routes(tmp_path, config):
    files = rgn.render(config)
    assert sorted(rgn.write(tmp_path, files)) == sorted(files)
    assert rgn.write(tmp_path, files) == []
    (tmp_path / "limits" / "retired.conf").write_text("old")
    assert rgn.write(tmp_path, files) == ["limits/retired.conf (removed)"]


def test_a_guard_local_yaml_is_merged_on_top(tmp_path):
    (tmp_path / "guard.yaml").write_text(GUARD_YAML.read_text())
    (tmp_path / rgn.LOCAL_NAME).write_text(yaml.safe_dump({"rate_limits": {"nginx": {"conn_per_client": 7}}}))
    assert "limit_conn hpldn_conn 7;" in rgn.render(rgn.load(tmp_path / "guard.yaml"))["server_limits.conf"]


def test_main_exit_status(tmp_path, capsys):
    assert rgn.main(["--config", str(GUARD_YAML), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "limits_http.conf").is_file()
    bad = tmp_path / "bad.yaml"
    bad.write_text("rate_limits: {nginx: {zones: {}}}\n")
    assert rgn.main(["--config", str(bad), "--out", str(tmp_path / "out2")]) == 1
    assert "crawler zone" in capsys.readouterr().err
