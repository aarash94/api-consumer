"""Scripted stand-ins for cluster nodes. No network, no sleeping."""

import httpx

from api_consumer.node_api import NodeAPI
from api_consumer.retry import RetryPolicy


class FakeCluster:
    """Every node answers from a per-host script, behind one shared httpx.Client.

    Hosts are keyed by their normalized URL, the same form the client reports.

    A script entry is an HTTP status code, an exception to raise, or a ready-made
    httpx.Response. A GET answered with a status code of 200 carries the group id
    taken from the request path. When a script runs out the fake raises IndexError,
    so an unexpected extra call fails the test loudly.
    """

    def __init__(self):
        self.mutations = {}
        self.gets = {}
        self.calls = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def node(self, host, *, mutations=(), gets=()):
        node = NodeAPI(host, self.client)
        self.mutations[node.host] = list(mutations)
        self.gets[node.host] = list(gets)
        return node

    def _handle(self, request):
        host = f"{request.url.scheme}://{request.url.netloc.decode()}"
        self.calls.append((request.method, host))
        script = self.gets[host] if request.method == "GET" else self.mutations[host]
        answer = script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        body = None
        if request.method == "GET" and answer == 200:
            body = {"groupId": request.url.path.split("/")[3]}
        return httpx.Response(answer, json=body)


def recording_policy(max_attempts=3):
    """A policy whose sleeps are appended to the returned list instead of being performed."""
    slept = []
    return RetryPolicy(max_attempts=max_attempts, sleep=slept.append), slept
