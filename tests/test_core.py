import json

from demondeploy import deploy, domains, proxy, sysd
from demondeploy.model import Project
from demondeploy.state import State


def mkproj(st, name="web", kind="node"):
    st.add_project(Project(name=name, source_type="dir", source="/tmp/x", kind=kind, start_cmd="npm start"))


def test_state_activate_and_env():
    st = State()
    mkproj(st)
    d1 = st.new_deployment("web", kind="node", port=20001)
    assert st.activate("web", d1.id) is None
    d2 = st.new_deployment("web", kind="node", port=20002)
    assert st.activate("web", d2.id) == d1.id
    assert st.deployment(d1.id).status == "superseded"
    assert st.project("web").active_id == d2.id
    st.set_env("web", "A", "1")
    assert st.env("web") == {"A": "1"}
    st.delete_project("web")
    assert st.env("web") == {} and st.deployments("web") == []


def test_proxy_config_local_only():
    st = State()
    mkproj(st)
    mkproj(st, "site", kind="static")
    d = st.new_deployment("web", kind="node", port=21000, unit="u")
    st.activate("web", d.id)
    s = st.new_deployment("site", kind="static", static_dir="/srv/site")
    st.activate("site", s.id)
    cfg = proxy.build_config(st)
    srv = cfg["apps"]["http"]["servers"]["http"]
    assert srv["listen"] == [":8080"]
    dumped = json.dumps(cfg)
    assert "127.0.0.1:21000" in dumped and "/srv/site" in dumped and "site.localhost" in dumped
    assert "tls" not in cfg["apps"]


def test_proxy_config_tls_and_domains():
    st = State()
    st.set_meta("http_port", "80")
    mkproj(st)
    d = st.new_deployment("web", kind="node", port=21000)
    st.activate("web", d.id)
    st.add_domain("web", "example.com")
    st.add_domain("web", "box.internal")
    cfg = proxy.build_config(st)
    servers = cfg["apps"]["http"]["servers"]
    assert servers["https"]["listen"] == [":443"]
    https_hosts = servers["https"]["routes"][0]["match"][0]["host"]
    assert set(https_hosts) == {"example.com", "box.internal"}
    pol = cfg["apps"]["tls"]["automation"]["policies"]
    assert pol[0]["subjects"] == ["box.internal"] and pol[0]["issuers"][0]["module"] == "internal"
    # http server redirects custom domains to https
    assert any(r.get("handle", [{}])[0].get("status_code") == 308 for r in servers["http"]["routes"])


def test_stopped_and_undeployed_get_placeholder():
    st = State()
    mkproj(st)
    cfg = json.dumps(proxy.build_config(st))
    assert "Not deployed yet" in cfg
    d = st.new_deployment("web", kind="node", port=21000)
    st.activate("web", d.id)
    st.update_project("web", stopped=True)
    assert "App stopped" in json.dumps(proxy.build_config(st))


def test_unit_quoting_and_env_line():
    assert sysd.q("/a b/%c") == '"/a b/%%c"'
    assert sysd.q("/plain") == "/plain"
    assert deploy._env_line("K", 'a"b$c\\') == 'K="a\\"b\\$c\\\\"\n'


def test_unit_project_parsing():
    names = {"my-app", "my"}
    assert deploy._unit_project("demon-app-my-app-12-abc123.service", names) == "my-app"
    assert deploy._unit_project("demon-app-my-3-abc123.service", names) == "my"
    assert deploy._unit_project("demon-app-other-3-abc123.service", names) is None


def test_domain_normalize():
    assert domains.normalize("HTTPS://Example.COM/path") == "example.com"
    for bad in ("foo", "-a.com", "x.localhost", "a..b"):
        try:
            domains.normalize(bad)
        except Exception:
            continue
        raise AssertionError(bad)


def test_rollback_target():
    import pytest
    from demondeploy import paths
    from demondeploy.proc import DemonError
    st = State()
    mkproj(st)
    ids = []
    for _ in range(3):
        d = st.new_deployment("web", kind="node")
        (paths.deployment_dir("web", d.id) / "src").mkdir(parents=True)
        st.activate("web", d.id)
        ids.append(d.id)
    assert deploy.rollback_target(st, "web").id == ids[1]
    assert deploy.rollback_target(st, "web", ids[0]).id == ids[0]
    with pytest.raises(DemonError):
        deploy.rollback_target(st, "web", ids[2])
