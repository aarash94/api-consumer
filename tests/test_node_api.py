"""Tests for the single-node API layer. No network: every answer is scripted."""

import json

import httpx
import pytest

from api_consumer.node_api import NodeAPI, NodeState, Outcome, TransientError, normalize_host


def make_node(handler):
    return NodeAPI("node1.example.com", httpx.Client(transport=httpx.MockTransport(handler)))


def respond(status, body=None):
    return lambda request: httpx.Response(status, json=body)


def fail(exc):
    def handler(request):
        raise exc

    return handler


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("node1.example.com", "http://node1.example.com"),
        ("node1.example.com:8080", "http://node1.example.com:8080"),
        ("https://node1.example.com/", "https://node1.example.com"),
    ],
)
def test_normalize_host_accepts_bare_hosts_and_root_urls(host, expected):
    assert normalize_host(host) == expected


@pytest.mark.parametrize(
    "host", ["", "http://node1/api", "http://node1/?x=1", "http://node1/#f", "ftp://node1"]
)
def test_normalize_host_rejects_everything_else(host):
    with pytest.raises(ValueError):
        normalize_host(host)


def test_get_uses_exact_path_and_reports_present():
    seen = {}

    def handler(request):
        seen["method"], seen["url"] = request.method, str(request.url)
        return httpx.Response(200, json={"groupId": "g/1"})

    assert make_node(handler).get_state("g/1") == NodeState.PRESENT
    assert seen == {"method": "GET", "url": "http://node1.example.com/v1/group/g%2F1/"}


def test_get_404_means_absent():
    assert make_node(respond(404)).get_state("g1") == NodeState.ABSENT


@pytest.mark.parametrize(
    "handler",
    [
        respond(200, {"groupId": "other"}),
        respond(200, {"x": 1}),
        respond(200, [1]),
        respond(204),
        respond(403),
    ],
)
def test_get_malformed_or_undocumented_answer_is_unknown(handler):
    assert make_node(handler).get_state("g1") == NodeState.UNKNOWN


@pytest.mark.parametrize(
    "handler",
    [respond(500), respond(503), fail(httpx.ConnectTimeout("t")), fail(httpx.ConnectError("c"))],
)
def test_get_transient_failure_raises_for_the_retry_layer(handler):
    with pytest.raises(TransientError):
        make_node(handler).get_state("g1")


def test_create_sends_documented_request_and_accepts_201():
    seen = {}

    def handler(request):
        seen["method"], seen["url"] = request.method, str(request.url)
        seen["type"], seen["body"] = request.headers["content-type"], json.loads(request.content)
        return httpx.Response(201)

    assert make_node(handler).create("g1") == Outcome.SUCCESS
    assert seen == {
        "method": "POST",
        "url": "http://node1.example.com/v1/group/",
        "type": "application/json",
        "body": {"groupId": "g1"},
    }


def test_delete_sends_json_body_and_accepts_200():
    seen = {}

    def handler(request):
        seen["method"], seen["url"] = request.method, str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200)

    assert make_node(handler).delete("g1") == Outcome.SUCCESS
    assert seen == {
        "method": "DELETE",
        "url": "http://node1.example.com/v1/group/",
        "body": {"groupId": "g1"},
    }


@pytest.mark.parametrize(
    ("call", "handler", "expected"),
    [
        ("create", respond(400), Outcome.CLARIFY),
        ("delete", respond(404), Outcome.CLARIFY),
        ("create", respond(500), Outcome.AMBIGUOUS),
        ("delete", respond(502), Outcome.AMBIGUOUS),
        ("create", fail(httpx.ReadTimeout("t")), Outcome.AMBIGUOUS),
        ("delete", fail(httpx.ConnectError("c")), Outcome.AMBIGUOUS),
        ("create", respond(200), Outcome.UNEXPECTED),
        ("delete", respond(201), Outcome.UNEXPECTED),
        ("create", respond(404), Outcome.UNEXPECTED),
        ("delete", respond(400), Outcome.UNEXPECTED),
    ],
)
def test_mutation_answers_are_classified_by_the_contract(call, handler, expected):
    assert getattr(make_node(handler), call)("g1") == expected
