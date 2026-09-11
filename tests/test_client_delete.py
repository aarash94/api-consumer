"""Tests for deleting a group across the cluster: the mirror image of create."""

import httpx
import pytest

from api_consumer.client import ClusterClient, ClusterOperationError
from api_consumer.node_api import NodeState
from fake_cluster import FakeCluster, recording_policy

ABSENT, PRESENT = NodeState.ABSENT, NodeState.PRESENT
A, B, C = "http://a", "http://b", "http://c"
HOSTS = [A, B, C]


def cluster_with(scripts):
    """Build a fake cluster and a client for hosts A, B, C from {host: (gets, mutations)}."""
    fake = FakeCluster()
    for host in HOSTS:
        gets, mutations = scripts.get(host, ([200], []))
        fake.node(host, gets=gets, mutations=mutations)
    policy, _ = recording_policy()
    return fake, ClusterClient(HOSTS, policy=policy, client=fake.client)


def test_happy_path_deletes_from_every_present_node_in_order():
    fake, client = cluster_with({host: ([200], [200]) for host in HOSTS})
    result = client.delete_group("g1")
    assert (result.operation, result.changed, result.skipped) == ("delete", HOSTS, [])
    assert fake.calls == [("GET", host) for host in HOSTS] + [("DELETE", host) for host in HOSTS]


def test_nodes_without_the_group_are_skipped():
    fake, client = cluster_with({A: ([404], []), B: ([200], [200]), C: ([200], [200])})
    result = client.delete_group("g1")
    assert (result.changed, result.skipped) == ([B, C], [A])
    assert ("DELETE", A) not in fake.calls


def test_failure_recreates_only_the_nodes_this_operation_deleted():
    fake, client = cluster_with({A: ([200], [200, 201]), B: ([200, 200], [404])})
    with pytest.raises(ClusterOperationError) as info:
        client.delete_group("g1")
    error = info.value
    assert error.failed_host == B
    assert error.compensation == {B: "restored", A: "restored"}
    assert error.states == {A: PRESENT, B: PRESENT, C: PRESENT}
    assert error.cluster_consistent is True
    assert fake.calls == [
        ("GET", A),
        ("GET", B),
        ("GET", C),
        ("DELETE", A),
        ("DELETE", B),
        ("GET", B),
        ("POST", A),
    ]
    assert str(error).startswith("delete 'g1' failed on http://b: DELETE was clarify")


def test_a_node_that_never_had_the_group_is_not_recreated_by_rollback():
    timeout = httpx.ReadTimeout("t")
    fake, client = cluster_with(
        {
            A: ([404], []),
            B: ([200], [200, 201]),
            C: ([200, 200, 200, 200], [timeout, timeout, timeout]),
        }
    )
    with pytest.raises(ClusterOperationError) as info:
        client.delete_group("g1")
    error = info.value
    assert error.failed_host == C
    assert error.compensation == {C: "restored", B: "restored"}
    assert error.states == {A: ABSENT, B: PRESENT, C: PRESENT}
    assert error.cluster_consistent is False
    assert ("POST", A) not in fake.calls


def test_rollback_timeout_is_still_a_restoration_when_get_confirms_it():
    fake, client = cluster_with(
        {
            A: ([200, 200], [200, httpx.ReadTimeout("t")]),
            B: ([200, 200], [404]),
        }
    )
    with pytest.raises(ClusterOperationError) as info:
        client.delete_group("g1")
    error = info.value
    assert error.compensation == {B: "restored", A: "restored"}
    assert error.cluster_consistent is True
    assert fake.calls.count(("POST", A)) == 1
    assert fake.calls[-2:] == [("POST", A), ("GET", A)]
