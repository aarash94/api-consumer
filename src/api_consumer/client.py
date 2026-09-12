"""Create or delete a group on every node of a cluster, with compensating rollback."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

import httpx

from api_consumer.node_api import NodeAPI, NodeState
from api_consumer.retry import RetryPolicy, drive_node_to, observe


@dataclass(frozen=True)
class OperationResult:
    """A completed operation: the nodes it changed and the nodes that already had the state."""

    operation: str
    group_id: str
    changed: list[str]
    skipped: list[str]


@dataclass(eq=False)
class ClusterOperationError(Exception):
    """The operation could not be completed. Says what failed and what the rollback achieved.

    `compensation` maps every node this operation touched to "restored", "failed" or
    "unknown". `states` holds the last verified state of every node.
    """

    operation: str
    group_id: str
    failed_host: str
    cause: str
    compensation: dict[str, str]
    states: dict[str, NodeState]

    @property
    def mutated(self) -> bool:
        """Whether any node was touched before the failure."""
        return bool(self.compensation)

    @property
    def cluster_consistent(self) -> bool | None:
        """True if every node verifiably holds the same state, None if any node is unknown."""
        if NodeState.UNKNOWN in self.states.values():
            return None
        return len(set(self.states.values())) == 1

    def __str__(self) -> str:
        undo = ", ".join(f"{host} {result}" for host, result in self.compensation.items())
        return (
            f"{self.operation} {self.group_id!r} failed on {self.failed_host}: {self.cause}; "
            f"rollback: {undo or 'nothing to undo'}"
        )


class ClusterClient:
    """Drives every node to the requested state, one node at a time, and rolls back on failure."""

    def __init__(
        self,
        hosts: Sequence[str],
        *,
        timeout: float = 5.0,
        policy: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._policy = policy or RetryPolicy()
        self._nodes = [NodeAPI(host, self._client) for host in hosts]
        if not self._nodes:
            raise ValueError("at least one host is required")
        seen = [node.host for node in self._nodes]
        if len(set(seen)) != len(seen):
            raise ValueError(f"duplicate hosts: {seen}")

    def close(self) -> None:
        """Close the HTTP client, if this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def create_group(self, group_id: str) -> OperationResult:
        """Create the group on every node where it is absent."""
        return self._apply("create", group_id, NodeState.PRESENT)

    def delete_group(self, group_id: str) -> OperationResult:
        """Delete the group from every node where it is present."""
        return self._apply("delete", group_id, NodeState.ABSENT)

    def _apply(self, operation: str, group_id: str, desired: NodeState) -> OperationResult:
        if not group_id.strip():
            raise ValueError("group_id must contain at least one non-whitespace character")
        before = {node.host: observe(node, group_id, self._policy) for node in self._nodes}
        for host, state in before.items():
            if state is NodeState.UNKNOWN:
                raise ClusterOperationError(
                    operation,
                    group_id,
                    host,
                    "preflight: node state could not be determined",
                    compensation={},
                    states=before,
                )
        old = NodeState.ABSENT if desired is NodeState.PRESENT else NodeState.PRESENT
        plan = [node for node in self._nodes if before[node.host] is old]
        skipped = [host for host, state in before.items() if state is desired]
        states = dict(before)
        attempted: list[NodeAPI] = []
        for node in plan:
            attempted.append(node)
            result = drive_node_to(node, group_id, desired, self._policy)
            states[node.host] = result.state
            if not result.reached:
                undo = self._compensate(attempted, group_id, before, states)
                raise ClusterOperationError(
                    operation, group_id, node.host, result.reason, compensation=undo, states=states
                )
        return OperationResult(operation, group_id, [node.host for node in plan], skipped)

    def _compensate(
        self,
        attempted: list[NodeAPI],
        group_id: str,
        before: dict[str, NodeState],
        states: dict[str, NodeState],
    ) -> dict[str, str]:
        """Drive every touched node back to its pre-operation state, last touched first."""
        outcome: dict[str, str] = {}
        for node in reversed(attempted):
            target = before[node.host]
            if states[node.host] is target:
                # verified in its old state already; nothing to send
                outcome[node.host] = "restored"
                continue
            result = drive_node_to(node, group_id, target, self._policy)
            states[node.host] = result.state
            if result.reached:
                outcome[node.host] = "restored"
            elif result.state is NodeState.UNKNOWN:
                outcome[node.host] = "unknown"
            else:
                outcome[node.host] = "failed"
        return outcome
