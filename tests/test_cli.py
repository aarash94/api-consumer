"""Tests for the command-line interface. The cluster is faked; the CLI only wires things up."""

import pytest

import api_consumer
from api_consumer import cli
from api_consumer.client import ClusterClient
from fake_cluster import FakeCluster, recording_policy

A, B, C = "http://a", "http://b", "http://c"


@pytest.fixture
def fake(monkeypatch):
    """Route the CLI's ClusterClient to a scripted cluster instead of the network."""
    cluster = FakeCluster()
    policy, _ = recording_policy()

    def build(hosts, *, timeout):
        return ClusterClient(hosts, policy=policy, client=cluster.client)

    monkeypatch.setattr(cli, "ClusterClient", build)
    monkeypatch.delenv("CLUSTER_HOSTS", raising=False)
    return cluster


def test_public_api_is_importable():
    assert api_consumer.ClusterClient is ClusterClient
    assert set(api_consumer.__all__) == {
        "ClusterClient",
        "ClusterOperationError",
        "NodeState",
        "OperationResult",
        "RetryPolicy",
    }


def test_help_exits_zero_and_lists_both_operations(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "create" in out and "delete" in out and "--hosts" in out


def test_successful_create_exits_zero_and_reports_the_nodes(fake, capsys):
    for host in (A, B, C):
        fake.node(host, gets=[404], mutations=[201])
    assert cli.main(["create", "g1", "--hosts", "a, b,c"]) == 0
    out = capsys.readouterr().out
    assert out == "create 'g1' done: changed http://a, http://b, http://c; skipped none\n"


def test_failed_delete_exits_one_with_the_rollback_report(fake, capsys):
    fake.node(A, gets=[200], mutations=[200, 201])
    fake.node(B, gets=[200, 200], mutations=[404])
    fake.node(C, gets=[200])
    assert cli.main(["delete", "g1", "--hosts", "a,b,c"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: delete 'g1' failed on http://b: DELETE was clarify")
    assert err.endswith("rollback: http://b restored, http://a restored\n")


def test_hosts_can_come_from_the_environment(fake, monkeypatch):
    monkeypatch.setenv("CLUSTER_HOSTS", "a,b")
    for host in (A, B):
        fake.node(host, gets=[404], mutations=[201])
    assert cli.main(["create", "g1"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "g1"],
        ["create", " ", "--hosts", "a"],
        ["create", "g1", "--hosts", "a,a"],
        ["rename", "g1", "--hosts", "a"],
    ],
)
def test_usage_errors_exit_two_before_any_call(fake, argv):
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    assert fake.calls == []
