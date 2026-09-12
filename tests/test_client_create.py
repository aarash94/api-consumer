"""Tests for creating a group across the cluster: preflight, plan, rollback."""

import httpx
import pytest

from api_consumer.client import ClusterClient, ClusterOperationError
from api_consumer.node_api import NodeState
from fake_cluster import FakeCluster, recording_policy

ABSENT, PRESENT, UNKNOWN = NodeState.ABSENT, NodeState.PRESENT, NodeState.UNKNOWN
A, B, C = "http://a", "http://b", "http://c"
HOSTS = [A, B, C]


def cluster_with(scripts):
    """Build a fake cluster and a client for hosts A, B, C from {host: (gets, mutations)}."""
    fake = FakeCluster()
    for host in HOSTS:
        gets, mutations = scripts.get(host, ([404], []))
        fake.node(host, gets=gets, mutations=mutations)
    policy, _ = recording_policy()
    return fake, ClusterClient(HOSTS, policy=policy, client=fake.client)


def test_happy_path_creates_on_every_absent_node_in_order():
    fake, client = cluster_with({host: ([404], [201]) for host in HOSTS})
    result = client.create_group("g1")
    assert (result.changed, result.skipped) == (HOSTS, [])
    assert fake.calls == [("GET", host) for host in HOSTS] + [("POST", host) for host in HOSTS]


def test_preflight_unknown_aborts_before_any_mutation():
    fake, client = cluster_with({B: ([503, 503, 503], [])})
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert (error.failed_host, error.mutated, error.compensation) == (B, False, {})
    assert error.cluster_consistent is None
    assert "preflight" in error.cause
    assert all(method == "GET" for method, _ in fake.calls)


def test_nodes_that_already_have_the_group_are_skipped():
    fake, client = cluster_with({A: ([200], []), B: ([404], [201]), C: ([404], [201])})
    result = client.create_group("g1")
    assert (result.changed, result.skipped) == ([B, C], [A])
    assert ("POST", A) not in fake.calls


def test_failure_rolls_back_only_the_nodes_this_operation_touched():
    fake, client = cluster_with({A: ([404], [201, 200]), B: ([404, 404], [400])})
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert error.failed_host == B
    assert error.compensation == {B: "restored", A: "restored"}
    assert error.states == {A: ABSENT, B: ABSENT, C: ABSENT}
    assert error.cluster_consistent is True and error.mutated is True
    assert fake.calls == [
        ("GET", A),
        ("GET", B),
        ("GET", C),
        ("POST", A),
        ("POST", B),
        ("GET", B),
        ("DELETE", A),
    ]
    assert str(error).startswith("create 'g1' failed on http://b: POST was clarify")


def test_a_group_that_existed_before_is_never_deleted_by_rollback():
    timeout = httpx.ReadTimeout("t")
    fake, client = cluster_with(
        {
            A: ([200], []),
            B: ([404], [201, 200]),
            C: ([404, 404, 404, 404], [timeout, timeout, timeout]),
        }
    )
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert error.failed_host == C
    assert error.compensation == {C: "restored", B: "restored"}
    assert error.states == {A: PRESENT, B: ABSENT, C: ABSENT}
    assert error.cluster_consistent is False
    assert ("DELETE", A) not in fake.calls


def test_rollback_that_cannot_be_verified_is_reported_as_unknown():
    fake, client = cluster_with(
        {
            A: ([404, 503, 503, 503], [201, httpx.ReadTimeout("t")]),
            B: ([404, 404], [400]),
        }
    )
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert error.compensation == {B: "restored", A: "unknown"}
    assert error.states[A] is UNKNOWN
    assert error.cluster_consistent is None
    assert str(error).endswith("rollback: http://b restored, http://a unknown")


@pytest.mark.parametrize("hosts", [[], ["a", "a"], ["node1", "http://node1/"]])
def test_empty_or_duplicate_hosts_are_rejected(hosts):
    with pytest.raises(ValueError):
        ClusterClient(hosts, client=FakeCluster().client)


@pytest.mark.parametrize("group_id", ["", "   "])
def test_blank_group_id_is_rejected_before_any_call(group_id):
    fake, client = cluster_with({})
    with pytest.raises(ValueError):
        client.create_group(group_id)
    assert fake.calls == []


def test_post_timeout_confirmed_by_get_counts_as_created():
    fake, client = cluster_with(
        {A: ([404], [201]), B: ([404, 200], [httpx.ReadTimeout("t")]), C: ([404], [201])}
    )
    result = client.create_group("g1")
    assert (result.changed, result.skipped) == (HOSTS, [])
    assert fake.calls.count(("POST", B)) == 1


def test_unresolved_node_is_reported_unknown_and_still_driven_back():
    fake, client = cluster_with({A: ([404], [201, 200]), B: ([404, 503, 503, 503], [500, 200])})
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert error.failed_host == B and error.cause.endswith("GET shows unknown")
    assert error.compensation == {B: "restored", A: "restored"}
    assert error.cluster_consistent is True
    assert fake.calls[-2:] == [("DELETE", B), ("DELETE", A)]


def test_rollback_retries_a_transient_failure_and_recovers():
    fake, client = cluster_with({A: ([404, 200], [201, 500, 200]), B: ([404, 404], [400])})
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert error.compensation == {B: "restored", A: "restored"}
    assert error.states == {A: ABSENT, B: ABSENT, C: ABSENT}
    assert fake.calls[-3:] == [("DELETE", A), ("GET", A), ("DELETE", A)]


def test_rollback_hard_failure_is_reported_as_failed():
    fake, client = cluster_with({A: ([404, 200], [201, 404]), B: ([404, 404], [400])})
    with pytest.raises(ClusterOperationError) as info:
        client.create_group("g1")
    error = info.value
    assert error.compensation == {B: "restored", A: "failed"}
    assert error.states == {A: PRESENT, B: ABSENT, C: ABSENT}
    assert error.cluster_consistent is False
    assert str(error).endswith("rollback: http://b restored, http://a failed")


def test_context_manager_closes_only_the_http_client_it_created():
    shared = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    with ClusterClient(["node1"], client=shared):
        pass
    assert not shared.is_closed
    with ClusterClient(["node1"]) as owned:
        pass
    assert owned._client.is_closed
