"""Reporting and update-status compatibility across TrueNAS API versions."""

from unittest.mock import Mock

import pytest

from custom_components.truenas.coordinator import TrueNASCoordinator

MIB = 1024 * 1024


def graph(name, **means):
    """Build a reporting response with named aggregation dimensions."""
    return {
        "name": name,
        "legend": ["time", *means],
        "aggregations": {"mean": means},
    }


@pytest.fixture
def coordinator():
    """Exercise polling methods without starting HA or opening a socket."""
    obj = object.__new__(TrueNASCoordinator)
    obj.ds = {"system_info": {}, "interface": {}, "disk": {}}
    obj.api = Mock()
    obj.api.connected.return_value = True
    obj.api.error = ""
    obj.api.query.side_effect = lambda method, **kwargs: (
        {"version": "25.10.7", "physmem": 8192 * MIB} if method == "system.info" else []
    )
    obj.host = "nas.example"
    obj._version_major = 0
    obj._version_minor = 0
    obj._is_virtual = False
    obj._systemstats_errored = []
    obj.get_systeminfo()
    obj.api.query.reset_mock(side_effect=True)
    return obj


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("TrueNAS-SCALE-24.10.2", (24, 10)),
        ("TrueNAS-SCALE-25.04.2", (25, 4)),
        ("25.10.7", (25, 10)),
        ("26.0.0", (26, 0)),
    ],
)
def test_version_formats(coordinator, version, expected):
    coordinator._version_major = coordinator._version_minor = 0
    coordinator.api.query.side_effect = lambda method, **kwargs: (
        {"version": version, "physmem": 8192 * MIB} if method == "system.info" else []
    )
    coordinator.get_systeminfo()
    assert (coordinator._version_major, coordinator._version_minor) == expected


@pytest.mark.parametrize(
    ("version", "method"),
    [
        ((24, 10), "reporting.netdata_get_data"),
        ((25, 4), "reporting.netdata_get_data"),
        ((25, 10), "reporting.get_data"),
        ((26, 0), "reporting.get_data"),
    ],
)
def test_reporting_endpoint_and_ordered_timestamps(coordinator, version, method):
    coordinator._version_major, coordinator._version_minor = version
    coordinator.api.query.return_value = []
    coordinator.get_systemstats()
    args, kwargs = coordinator.api.query.call_args
    assert args == (method,)
    graphs, query = kwargs["params"]
    assert {g["name"] for g in graphs} == {
        "load",
        "cputemp",
        "cpu",
        "arcsize",
        "memory",
    }
    assert isinstance(query["start"], int)
    assert query["end"] - query["start"] == 60
    assert query["aggregate"] is True


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (graph("cpu", cpu=31.3934, cpu0=99), 31.39),
        (graph("cpu", cpu=0), 0),
        (graph("cpu", cpu=None), None),
        (graph("cpu", system=7.5, user=12.5, idle=80), 20),
    ],
)
def test_cpu_response_formats(coordinator, response, expected):
    coordinator.api.query.return_value = [response]
    coordinator.get_systemstats()
    assert coordinator.ds["system_info"]["cpu_usage"] == expected


def test_available_memory_is_bytes(coordinator):
    coordinator.api.query.return_value = [graph("memory", available=2048 * MIB)]
    coordinator.get_systemstats()
    info = coordinator.ds["system_info"]
    assert info["memory-total_value"] == 8192 * MIB
    assert info["memory-free_value"] == 2048 * MIB
    assert info["memory-used_value"] == 6144 * MIB
    assert info["memory-usage_percent"] == 75


def test_legacy_memory_is_mebibytes(coordinator):
    coordinator.api.query.return_value = [
        graph("memory", free=2048, used=4096, cached=1024, buffers=1024)
    ]
    coordinator.get_systemstats()
    info = coordinator.ds["system_info"]
    assert info["memory-free_value"] == 2048 * MIB
    assert info["memory-used_value"] == 4096 * MIB
    assert info["memory-cached_value"] == 1024 * MIB
    assert info["memory-buffered_value"] == 1024 * MIB
    assert info["memory-usage_percent"] == 75


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (graph("arcsize", size=2493169541), 2493169541),
        (graph("arcsize", arc_size=2048), 2048 * MIB),
        (graph("arcsize", size=0), 0),
    ],
)
def test_arc_response_formats(coordinator, response, expected):
    coordinator.api.query.return_value = [response]
    coordinator.get_systemstats()
    assert coordinator.ds["system_info"]["cache_size-arc_value"] == expected


def test_graph_failure_isolation_is_bounded_and_keeps_valid_results(coordinator):
    coordinator.api.error = 500

    def query(method, params):
        graphs, options = params
        assert options["start"] < options["end"]
        assert options["aggregate"] is True
        if len(graphs) > 1 or graphs[0]["name"] == "cputemp":
            return None
        return [graph("cpu", cpu=25)] if graphs[0]["name"] == "cpu" else []

    coordinator.api.query.side_effect = query
    coordinator.get_systemstats()
    assert coordinator.api.query.call_count == 6
    assert coordinator._systemstats_errored == ["cputemp"]
    assert coordinator.ds["system_info"]["cpu_usage"] == 25


def test_excluded_adjacent_graphs_are_both_removed(coordinator):
    coordinator._systemstats_errored = ["load", "cputemp"]
    coordinator.api.query.return_value = []
    coordinator.get_systemstats()
    graphs = coordinator.api.query.call_args.kwargs["params"][0]
    assert [g["name"] for g in graphs] == ["cpu", "arcsize", "memory"]


@pytest.mark.parametrize("version", [(24, 10), (25, 4)])
def test_legacy_update_check(coordinator, version):
    coordinator._version_major, coordinator._version_minor = version
    coordinator.api.query.return_value = {"status": "AVAILABLE", "version": "next"}
    coordinator.get_updatecheck()
    coordinator.api.query.assert_called_once_with("update.check_available")
    assert coordinator.ds["system_info"]["update_available"] is True
    assert coordinator.ds["system_info"]["update_version"] == "next"


@pytest.mark.parametrize("version", [(25, 10), (26, 0)])
@pytest.mark.parametrize("new_version", [None, {"version": "next"}])
def test_current_update_status(coordinator, version, new_version):
    coordinator._version_major, coordinator._version_minor = version
    coordinator.api.query.return_value = {
        "code": "NORMAL",
        "status": {"new_version": new_version},
    }
    coordinator.get_updatecheck()
    coordinator.api.query.assert_called_once_with("update.status")
    info = coordinator.ds["system_info"]
    assert info["update_available"] is bool(new_version)
    assert info["update_version"] == ("next" if new_version else "25.10.7")


@pytest.mark.parametrize("response", [None, {"code": "ERROR", "status": None}])
def test_failed_update_check_does_not_erase_known_update(coordinator, response):
    info = coordinator.ds["system_info"]
    info.update(update_available=True, update_version="next")
    coordinator.api.query.return_value = response
    coordinator.get_updatecheck()
    assert info["update_status"] == "unknown"
    assert info["update_available"] is True
    assert info["update_version"] == "next"


def test_disk_temperatures_need_no_version_specific_argument_names(coordinator):
    coordinator.api.query.side_effect = lambda method, **kwargs: (
        [{"identifier": "disk-1", "name": "sda"}]
        if method == "disk.query"
        else {"sda": 36}
    )
    coordinator.get_disk()
    assert coordinator.ds["disk"]["disk-1"]["temperature"] == 36
    assert coordinator.api.query.call_args.args == ("disk.temperatures",)
    assert coordinator.api.query.call_args.kwargs == {"params": {}}
