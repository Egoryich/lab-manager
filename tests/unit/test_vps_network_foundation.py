import importlib.util
import io
import ipaddress
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "vps_network_check",
    Path(__file__).resolve().parents[2] / "tools/verify-vps-network-foundation.py",
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_default_route_is_not_an_allocated_network_but_broad_route_is():
    pool = ipaddress.ip_network("10.70.0.0/16")
    assert check.overlapping_ranges(pool, [{"dst": "default"}], [], []) == []
    assert check.overlapping_ranges(pool, [{"dst": "10.0.0.0/8"}], [], []) == ["route 10.0.0.0/8"]


def test_vps_interface_and_docker_overlap_are_reported():
    pool = ipaddress.ip_network("10.70.0.0/16")
    addresses = [
        {
            "ifname": "test0",
            "addr_info": [{"family": "inet", "local": "10.70.1.1", "prefixlen": 24}],
        }
    ]
    docker_networks = [{"Name": "guest", "IPAM": {"Config": [{"Subnet": "10.70.8.0/24"}]}}]
    assert check.overlapping_ranges(pool, [], addresses, docker_networks) == [
        "interface test0 10.70.1.0/24",
        "Docker guest 10.70.8.0/24",
    ]


def test_lesson_api_check_uses_mounted_openapi_path(monkeypatch):
    class Response(io.BytesIO):
        status = 200

        def geturl(self):
            return "http://127.0.0.1:18000/api/openapi.json"

    class Opener:
        def open(self, url, timeout):
            assert url == "http://127.0.0.1:18000/api/openapi.json"
            assert timeout == 10
            return Response(
                json.dumps({"paths": dict.fromkeys(check.REQUIRED_API_ROUTES, {})}).encode()
            )

    monkeypatch.setattr(check, "OPENER", Opener())
    assert "lesson preview" in check.check_api_routes()
