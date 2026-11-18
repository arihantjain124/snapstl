"""The Bambuddy computer run from SnapSTL (bambuddy_host) and Bambuddy's Bambu Cloud list bug (slicing).

Windows services and Docker are faked (platform_os / module functions); nothing on this computer is touched.
"""
import pytest

from snapstl import bambuddy_host as H
from snapstl import farm

from .test_flow import client  # noqa: F401  (fixture)


@pytest.fixture
def host(monkeypatch):
    """A fake Bambuddy computer: service + Docker + sidecars, with every action recorded."""
    st = {"service": "running", "docker": True, "containers": {n: "running" for n in H.SIDECARS}, "calls": [],
          "ours": True, "octo": {"X1": "running"}}
    monkeypatch.setattr(H, "octo_plugins", lambda: [{"name": n, "state": s, "status": ""} for n, s in st["octo"].items()])
    monkeypatch.setattr(H.platform_os, "octoeverywhere_manager", lambda: {"installed": True, "running": False})
    monkeypatch.setattr(H.platform_os, "docker_cli", lambda: "docker")
    monkeypatch.setattr(H.platform_os, "open_octoeverywhere_manager", lambda: st["calls"].append(("open-octo",)) or True)

    def docker_restart():
        st["calls"].append(("docker-restart",))
        return True

    monkeypatch.setattr(H.platform_os, "restart_docker_desktop", docker_restart)
    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    monkeypatch.setattr(H, "is_here", lambda: True)
    monkeypatch.setattr(H, "ours", lambda: st["ours"])
    monkeypatch.setattr(H.platform_os, "service_state", lambda name="Bambuddy": st["service"])
    monkeypatch.setattr(H, "docker_running", lambda: st["docker"])
    monkeypatch.setattr(H, "sidecars", lambda: [{"name": n, "state": s, "health": "", "status": ""}
                                                for n, s in st["containers"].items()])
    monkeypatch.setattr(H, "_answers", lambda url, timeout=3: True)

    def control(action, name="Bambuddy", elevate=False):
        st["calls"].append(("service", action, elevate))
        st["service"] = "running"
        return ""

    def docker_desktop():
        st["calls"].append(("docker-desktop",))
        st["docker"] = True
        return True

    def run(cmd, timeout=60, cwd=None, env=None):
        if cmd[1] in ("start", "restart"):  # docker start / restart <octo plugins>
            st["calls"].append(("octo", cmd[1], tuple(cmd[2:])))
            st["octo"] = {n: "running" for n in st["octo"]}
            return 0, ""
        st["calls"].append(("compose", tuple(cmd[-2:]) if "--force-recreate" not in cmd else ("recreate",)))
        st["containers"] = {n: "running" for n in H.SIDECARS}
        return 0, ""

    monkeypatch.setattr(H.platform_os, "control_service", control)
    monkeypatch.setattr(H.platform_os, "start_docker_desktop", docker_desktop)
    monkeypatch.setattr(H.platform_os, "run_quiet", run)
    monkeypatch.setattr(H, "compose_cmd", lambda: (["docker", "compose"], H.ROOT, {}))
    farm.update_config(keep_bambuddy_running=True)
    h = H.Host()
    monkeypatch.setattr(H, "HOST", h)
    st["host"] = h
    return st


def _wait(h):
    if h._thread:
        h._thread.join(10)


def test_all_running_nothing_is_touched(host):
    h = host["host"]
    s = h.status()
    assert s["here"] and s["ok"]
    h.tick()
    _wait(h)
    assert host["calls"] == []  # Keep running never restarts what's running


def test_keep_running_starts_what_stopped_without_admin_prompt(host):
    host["service"] = "stopped"
    host["docker"] = False
    host["containers"] = {n: "exited" for n in H.SIDECARS}
    h = host["host"]
    h.tick()
    _wait(h)
    assert ("service", "start", False) in host["calls"]  # never elevate by itself
    assert ("docker-desktop",) in host["calls"]
    assert ("compose", ("up", "-d")) in host["calls"]
    assert not any(c[0] == "service" and c[1] == "restart" for c in host["calls"])
    assert h.status()["ok"]


def test_keep_running_off_does_nothing(host):
    host["service"] = "stopped"
    farm.update_config(keep_bambuddy_running=False)
    host["host"].tick()
    _wait(host["host"])
    assert host["calls"] == []


def test_start_click_may_ask_for_admin(host):
    host["service"] = "stopped"
    h = host["host"]
    assert h.start(elevate=True)
    _wait(h)
    assert host["calls"][0] == ("service", "start", True)


def test_restart_only_our_bambuddy(host):
    h = host["host"]
    host["ours"] = False
    with pytest.raises(PermissionError):
        h.restart_bambuddy()
    host["ours"] = True
    assert h.restart_bambuddy()
    _wait(h)
    assert ("service", "restart", True) in host["calls"]
    assert h.restart_slicer()
    _wait(h)
    assert ("compose", ("recreate",)) in host["calls"]


def test_not_on_other_computers():
    # tests run with SNAPSTL_NO_BAMBUDDY=1: never the real Bambuddy / Docker of the computer running them
    assert H.is_here() is False
    assert H.HOST.status() == {"here": False, "busy": "", "log": [], "keep_running": H.keep_running()}


def test_endpoints(client, host):
    assert client.get("/api/bambuddy-host").json()["ok"]
    assert client.post("/api/bambuddy-host/keep-running", json={"on": False}).json() == {"keep_running": False}
    assert farm.load_config()["keep_bambuddy_running"] is False
    assert client.post("/api/bambuddy-host/start", json={}).status_code == 200
    _wait(host["host"])
    assert client.post("/api/bambuddy-host/nope", json={}).status_code == 404


# ---- Bambuddy's Bambu Cloud list bug ----
def test_cloud_list_bug_is_explained():
    raw = "Unexpected error: 'NoneType' object has no attribute 'get'"
    hint = farm.slicer_hint(raw)
    assert "Bambu Cloud" in hint and "sign out" in hint and "local" in hint and raw in hint
    assert farm.slicer_hint("some other problem") == "some other problem"


def test_preset_list_crash_is_explained(monkeypatch):
    def boom(*a, **k):
        raise farm.FarmError("Bambuddy: Internal Server Error")

    monkeypatch.setattr(farm, "api", boom)
    with pytest.raises(farm.FarmError, match="Bambu Cloud"):
        farm.bambuddy_presets({})



def test_own_profiles_still_listed_when_bambuddys_list_crashes(monkeypatch):
    def api(method, path, **k):
        if path.startswith("/slicer/presets"):
            raise farm.FarmError("Bambuddy: Internal Server Error")
        assert path == "/local-presets/"
        return {"printer": [{"id": 4, "name": "SnapSTL A1 0.4 nozzle"}], "process": [], "filament": None}

    monkeypatch.setattr(farm, "api", api)
    listed, note = farm.preset_listing({})
    assert listed["printer"] == [{"key": "local:4", "name": "SnapSTL A1 0.4 nozzle", "source": "local"}]
    assert listed["process"] == listed["filament"] == []
    assert "Bambu Cloud" in note
    assert farm.bambuddy_presets({}) == listed  # saving a pick checks against the same list


def test_other_bambuddy_errors_are_not_hidden_by_the_local_list(monkeypatch):
    def api(method, path, **k):
        assert path.startswith("/slicer/presets")
        raise farm.FarmError("Can't reach Bambuddy")

    monkeypatch.setattr(farm, "api", api)
    with pytest.raises(farm.FarmError, match="Can't reach"):
        farm.preset_listing({})


def test_octoeverywhere_checked_started_and_restarted(host):
    h = host["host"]
    s = h.status()
    assert s["octoeverywhere"] == {"installed": True, "running": False, "plugins": [{"name": "X1", "state": "running", "status": ""}]}
    host["octo"]["X1"] = "exited"
    assert not h.status()["ok"]
    h.tick()  # Keep running starts a stopped plugin (start, never restart)
    _wait(h)
    assert ("octo", "start", ("X1",)) in host["calls"] and h.status()["ok"]
    assert h.restart_octoeverywhere()
    _wait(h)
    assert ("octo", "restart", ("X1",)) in host["calls"]
    assert h.open_octoeverywhere() and ("open-octo",) in host["calls"]


def test_restart_all_and_docker(client, host):
    h = host["host"]
    assert client.post("/api/bambuddy-host/restart-all", json={}).status_code == 200
    _wait(h)
    calls = host["calls"]
    assert ("service", "restart", True) in calls and ("compose", ("recreate",)) in calls
    assert ("octo", "restart", ("X1",)) in calls and ("docker-restart",) not in calls  # Docker has its own button
    host["calls"].clear()
    host["ours"] = False  # a Bambuddy SnapSTL didn't install is never restarted, even by Restart all
    h.restart_all()
    _wait(h)
    assert not any(c[0] == "service" for c in host["calls"]) and "not installed by SnapSTL" in h.log[-3]
    assert client.post("/api/bambuddy-host/restart-docker", json={}).status_code == 200
    _wait(h)
    assert ("docker-restart",) in host["calls"]
    assert client.post("/api/bambuddy-host/open-octoeverywhere", json={}).status_code == 200
