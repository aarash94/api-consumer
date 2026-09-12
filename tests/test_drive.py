"""Tests for driving one node to a desired state. Never a blind mutation retry."""

import httpx
import pytest

from api_consumer.node_api import NodeState
from api_consumer.retry import RetryPolicy, drive_node_to
from fake_cluster import FakeCluster, recording_policy

PRESENT, ABSENT, UNKNOWN = NodeState.PRESENT, NodeState.ABSENT, NodeState.UNKNOWN
TIMEOUT = httpx.ReadTimeout("t")


def drive(desired, *, mutations, gets=()):
    cluster = FakeCluster()
    node = cluster.node("node1", mutations=mutations, gets=gets)
    policy, slept = recording_policy()
    result = drive_node_to(node, "g1", desired, policy)
    return result, [method for method, _ in cluster.calls], slept


def test_documented_success_is_trusted_without_a_get():
    result, calls, slept = drive(PRESENT, mutations=[201])
    assert (result.reached, result.state) == (True, PRESENT)
    assert calls == ["POST"] and slept == []


def test_timeout_then_get_present_is_success_with_no_second_post():
    result, calls, _ = drive(PRESENT, mutations=[TIMEOUT], gets=[200])
    assert (result.reached, result.state) == (True, PRESENT)
    assert calls == ["POST", "GET"]


def test_timeout_then_get_absent_allows_one_bounded_retry():
    result, calls, slept = drive(PRESENT, mutations=[TIMEOUT, 201], gets=[404])
    assert result.reached
    assert calls == ["POST", "GET", "POST"] and slept == [0.1]


def test_ambiguous_answer_then_get_unknown_is_unresolved_with_no_second_post():
    result, calls, _ = drive(PRESENT, mutations=[500], gets=[503, 503, 503])
    assert (result.reached, result.state) == (False, UNKNOWN)
    assert calls == ["POST", "GET", "GET", "GET"]


def test_ambiguous_answers_stop_after_the_attempt_budget():
    result, calls, slept = drive(PRESENT, mutations=[500, TIMEOUT, 502], gets=[404, 404, 404])
    assert (result.reached, result.state) == (False, ABSENT)
    assert calls == ["POST", "GET"] * 3 and slept == [0.1, 0.2]
    assert "attempt 3 of 3" in result.reason


def test_post_400_then_get_present_is_success():
    result, calls, _ = drive(PRESENT, mutations=[400], gets=[200])
    assert (result.reached, result.state) == (True, PRESENT)
    assert calls == ["POST", "GET"]


def test_post_400_then_get_absent_is_a_hard_failure_without_retry():
    result, calls, slept = drive(PRESENT, mutations=[400], gets=[404])
    assert (result.reached, result.state) == (False, ABSENT)
    assert calls == ["POST", "GET"] and slept == []


def test_undocumented_status_is_checked_once_and_never_retried():
    result, calls, slept = drive(PRESENT, mutations=[204], gets=[404])
    assert (result.reached, result.state) == (False, ABSENT)
    assert calls == ["POST", "GET"] and slept == []


def test_delete_success_is_trusted():
    result, calls, _ = drive(ABSENT, mutations=[200])
    assert (result.reached, result.state) == (True, ABSENT)
    assert calls == ["DELETE"]


def test_delete_timeout_then_get_present_allows_one_bounded_retry():
    result, calls, slept = drive(ABSENT, mutations=[TIMEOUT, 200], gets=[200])
    assert result.reached
    assert calls == ["DELETE", "GET", "DELETE"] and slept == [0.1]


def test_delete_404_then_get_absent_is_success():
    result, calls, _ = drive(ABSENT, mutations=[404], gets=[404])
    assert (result.reached, result.state) == (True, ABSENT)
    assert calls == ["DELETE", "GET"]


def test_delete_404_then_get_present_is_a_hard_failure_without_retry():
    result, calls, slept = drive(ABSENT, mutations=[404], gets=[200])
    assert (result.reached, result.state) == (False, PRESENT)
    assert calls == ["DELETE", "GET"] and slept == []


def test_unknown_is_not_a_valid_target():
    with pytest.raises(ValueError):
        drive(UNKNOWN, mutations=[])


def test_delete_timeout_then_get_absent_is_success_with_no_second_delete():
    result, calls, _ = drive(ABSENT, mutations=[TIMEOUT], gets=[404])
    assert (result.reached, result.state) == (True, ABSENT)
    assert calls == ["DELETE", "GET"]


def test_a_policy_without_attempts_is_rejected():
    node = FakeCluster().node("node1")
    with pytest.raises(ValueError):
        drive_node_to(node, "g1", PRESENT, RetryPolicy(max_attempts=0, sleep=lambda _: None))
