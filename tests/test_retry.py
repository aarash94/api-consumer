"""Tests for bounded observation. Sleeps are recorded, never performed."""

import httpx

from api_consumer.node_api import NodeAPI, NodeState
from api_consumer.retry import RetryPolicy, observe


def scripted_node(*answers):
    """A node whose GET answers follow the script: a status code or an exception to raise."""
    script = list(answers)

    def handler(request):
        answer = script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"groupId": "g1"} if answer == 200 else None)

    return NodeAPI("node1", httpx.Client(transport=httpx.MockTransport(handler)))


def recording_policy(max_attempts=3):
    slept = []
    return RetryPolicy(max_attempts=max_attempts, sleep=slept.append), slept


def test_transient_failure_then_200_is_present_after_one_backoff():
    policy, slept = recording_policy()
    node = scripted_node(httpx.ReadTimeout("t"), 200)
    assert observe(node, "g1", policy) == NodeState.PRESENT
    assert slept == [0.1]


def test_transient_failures_then_404_is_absent_with_growing_backoff():
    policy, slept = recording_policy()
    node = scripted_node(500, httpx.ConnectError("c"), 404)
    assert observe(node, "g1", policy) == NodeState.ABSENT
    assert slept == [0.1, 0.2]


def test_exhaustion_is_unknown_and_does_not_sleep_after_the_last_attempt():
    policy, slept = recording_policy()
    node = scripted_node(503, 503, 503)
    assert observe(node, "g1", policy) == NodeState.UNKNOWN
    assert slept == [0.1, 0.2]


def test_malformed_answer_is_unknown_without_retry():
    policy, slept = recording_policy()
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"groupId": "someone-else"})

    node = NodeAPI("node1", httpx.Client(transport=httpx.MockTransport(handler)))
    assert observe(node, "g1", policy) == NodeState.UNKNOWN
    assert calls == ["/v1/group/g1/"]
    assert slept == []


def test_delay_doubles_per_attempt():
    policy = RetryPolicy(base_delay=0.1)
    assert [policy.delay(attempt) for attempt in (1, 2, 3)] == [0.1, 0.2, 0.4]
