"""Bounded observation of a node's state, with a small retry policy shared by all callers."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from api_consumer.node_api import NodeAPI, NodeState, TransientError


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
