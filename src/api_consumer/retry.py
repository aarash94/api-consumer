"""Bounded loops over one node: observe its state, or drive it to a desired state."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from api_consumer.node_api import NodeAPI, NodeState, Outcome, TransientError


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts to make and how long to wait between them: 0.1 s, then 0.2 s, ..."""

    max_attempts: int = 3
    base_delay: float = 0.1
    sleep: Callable[[float], None] = time.sleep

    def delay(self, attempt: int) -> float:
        """Seconds to wait after the given 1-based attempt failed. Doubles each time."""
        return self.base_delay * 2 ** (attempt - 1)


def observe(node: NodeAPI, group_id: str, policy: RetryPolicy) -> NodeState:
    """Ask a node for the group's state, retrying only transient failures."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return node.get_state(group_id)
        except TransientError:
            if attempt < policy.max_attempts:
                policy.sleep(policy.delay(attempt))
    return NodeState.UNKNOWN


@dataclass(frozen=True)
class DriveResult:
    """Whether the node reached the desired state, what it verifiably holds now, and why."""

    reached: bool
    state: NodeState
    reason: str


def drive_node_to(
    node: NodeAPI, group_id: str, desired: NodeState, policy: RetryPolicy
) -> DriveResult:
    """Send the mutation for `desired` and settle its outcome by the contract rules.

    A documented success is trusted. Every other answer is followed by GET, and the
    observed state decides. The mutation is sent again only after an ambiguous answer,
    when GET shows the node still in its old state, and while attempts remain.
    """
    if desired is NodeState.UNKNOWN:
        raise ValueError("desired state must be PRESENT or ABSENT")
    creating = desired is NodeState.PRESENT
    mutate = node.create if creating else node.delete
    verb = "POST" if creating else "DELETE"
    old = NodeState.ABSENT if creating else NodeState.PRESENT
    for attempt in range(1, policy.max_attempts + 1):
        outcome = mutate(group_id)
        if outcome is Outcome.SUCCESS:
            return DriveResult(True, desired, f"{verb} succeeded")
        state = observe(node, group_id, policy)
        why = (
            f"{verb} was {outcome.value} (attempt {attempt} of {policy.max_attempts}), "
            f"GET shows {state.value}"
        )
        if state is desired:
            return DriveResult(True, desired, why)
        if outcome is Outcome.AMBIGUOUS and state is old and attempt < policy.max_attempts:
            policy.sleep(policy.delay(attempt))
            continue
        return DriveResult(False, state, why)
    raise ValueError(f"max_attempts must be at least 1, got {policy.max_attempts}")
