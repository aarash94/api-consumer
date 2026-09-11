"""HTTP calls against one cluster node, exactly as the API documents them."""

from enum import Enum
from urllib.parse import quote, urlsplit

import httpx


class NodeState(Enum):
    """What a node holds for one group, as far as it could be observed."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Outcome(Enum):
    """Classification of one POST or DELETE answer."""

    SUCCESS = "success"  # documented success code
    CLARIFY = "clarify"  # POST 400 / DELETE 404: check with GET, never repeat the call
    AMBIGUOUS = "ambiguous"  # timeout, connection failure or 5xx: the change may have been applied
    UNEXPECTED = "unexpected"  # any other status: the node broke the contract


class TransientError(Exception):
    """GET could not be completed (timeout, connection failure or 5xx); a retry may help."""


def normalize_host(host: str) -> str:
    """Accept a bare hostname or a root http(s) URL, optionally with a port."""
    raw = host if "://" in host else f"http://{host}"
    parts = urlsplit(raw)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"host must be a bare hostname or a root http(s) URL: {host!r}")
    return f"{parts.scheme}://{parts.netloc}"


class NodeAPI:
    """The three documented calls for one node. The caller owns the client and its timeout."""

    def __init__(self, host: str, client: httpx.Client) -> None:
        self.host = normalize_host(host)
        self._client = client

    def get_state(self, group_id: str) -> NodeState:
        url = f"{self.host}/v1/group/{quote(group_id, safe='')}/"
        try:
            response = self._client.get(url)
        except httpx.TransportError as exc:
            raise TransientError(f"GET {url}: {exc}") from exc
        if response.status_code == 404:
            return NodeState.ABSENT
        if response.status_code >= 500:
            raise TransientError(f"GET {url}: HTTP {response.status_code}")
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                return NodeState.UNKNOWN
            if isinstance(body, dict) and body.get("groupId") == group_id:
                return NodeState.PRESENT
        return NodeState.UNKNOWN

    def create(self, group_id: str) -> Outcome:
        return self._mutate("POST", group_id, success=201, clarify=400)

    def delete(self, group_id: str) -> Outcome:
        return self._mutate("DELETE", group_id, success=200, clarify=404)

    def _mutate(self, method: str, group_id: str, success: int, clarify: int) -> Outcome:
        try:
            response = self._client.request(
                method, f"{self.host}/v1/group/", json={"groupId": group_id}
            )
        except httpx.TransportError:
            return Outcome.AMBIGUOUS
        if response.status_code == success:
            return Outcome.SUCCESS
        if response.status_code == clarify:
            return Outcome.CLARIFY
        if response.status_code >= 500:
            return Outcome.AMBIGUOUS
        return Outcome.UNEXPECTED
