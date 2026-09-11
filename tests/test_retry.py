"""Tests for bounded observation. Sleeps are recorded, never performed."""

import httpx

from api_consumer.node_api import NodeState
from api_consumer.retry import RetryPolicy, observe
from fake_cluster import FakeCluster, recording_policy


def observe_with(*gets):
    cluster = FakeCluster()
    node = cluster.node("node1", gets=gets)
    policy, slept = recording_policy()
    return observe(node, "g1", policy), len(cluster.calls), slept


def test_transient_failure_then_200_is_present_after_one_backoff():
    assert observe_with(httpx.ReadTimeout("t"), 200) == (NodeState.PRESENT, 2, [0.1])


def test_transient_failures_then_404_is_absent_with_growing_backoff():
    assert observe_with(500, httpx.ConnectError("c"), 404) == (NodeState.ABSENT, 3, [0.1, 0.2])


def test_exhaustion_is_unknown_and_does_not_sleep_after_the_last_attempt():
    assert observe_with(503, 503, 503) == (NodeState.UNKNOWN, 3, [0.1, 0.2])


def test_malformed_answer_is_unknown_without_retry():
    wrong_group = httpx.Response(200, json={"groupId": "someone-else"})
    assert observe_with(wrong_group) == (NodeState.UNKNOWN, 1, [])


def test_delay_doubles_per_attempt():
    policy = RetryPolicy(base_delay=0.1)
    assert [policy.delay(attempt) for attempt in (1, 2, 3)] == [0.1, 0.2, 0.4]
