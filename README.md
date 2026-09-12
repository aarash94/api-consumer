# api-consumer

A Python client that creates or deletes a group on every node of a cluster and rolls its own changes back when a node fails. Written for the "API consumer" coding challenge. The cluster API itself is out of scope; this repository is the client only.

## What it does

A group exists in the cluster when every node has a record of it. Each node exposes the same REST API:

| Call | Request | Documented answers |
|---|---|---|
| create | `POST /v1/group/` with `{"groupId": "..."}` | `201`; `400` may mean the group already exists |
| delete | `DELETE /v1/group/` with `{"groupId": "..."}` | `200` |
| read | `GET /v1/group/{groupId}/` | `200` with `{"groupId": "..."}`; `404` not found |

The API is unreliable: any call can time out or answer with a 5xx. So the client reads every node before it changes anything, changes one node at a time, checks with GET whenever an answer is ambiguous, and on failure drives the nodes it touched back to the state it found them in. The outcome is either "done", with the nodes that were changed and the nodes that already had the requested state, or an error that says which node failed, why, and what the rollback achieved on every touched node.

## Running it

Python 3.12 or newer. httpx is the only runtime dependency.

```
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Command line:

```
api-consumer create my-group --hosts node1.example.com,node2.example.com,node3.example.com
api-consumer delete my-group --hosts node1.example.com,node2.example.com,node3.example.com
```

Hosts are bare hostnames, `host:port`, or `http://` / `https://` root URLs. They can also come from the `CLUSTER_HOSTS` environment variable. `--timeout` is the per-request timeout in seconds, default 5. Exit codes: 0 done, 1 the operation failed and the rollback report is on stderr, 2 wrong usage.

```
$ api-consumer create my-group --hosts node1,node2
create 'my-group' done: changed http://node1, http://node2; skipped none

$ api-consumer create my-group --hosts 127.0.0.1:9
error: create 'my-group' failed on http://127.0.0.1:9: preflight: node state could not be determined; rollback: nothing to undo
```

From Python:

```python
from api_consumer import ClusterClient, ClusterOperationError

with ClusterClient(["node1.example.com", "node2.example.com"], timeout=5.0) as client:
    try:
        result = client.create_group("my-group")
        print(result.changed, result.skipped)
    except ClusterOperationError as error:
        print(error.failed_host, error.cause)
        print(error.compensation, error.cluster_consistent)
```

`error.compensation` maps every node this operation touched to `restored`, `failed` or `unknown`. `error.states` holds the last verified state of every node, and `error.cluster_consistent` is `True`, `False`, or `None` when some node could not be verified.

## How it works

The whole design rests on three words for what a node holds for a given group: `PRESENT`, `ABSENT` and `UNKNOWN`. `UNKNOWN` means the client could not prove either state: GET failed three times in a row, or answered with something that is not in the contract.

**Read before write.** The client asks every node for the group's state first. If any node is `UNKNOWN`, it stops there and changes nothing. You cannot safely undo later what you could not see now.

**Only touch what needs touching.** Create sends POST only to nodes where the group is absent, delete sends DELETE only to nodes where it is present. Nodes already in the requested state are reported as skipped. Running the same command again after a failure or a crash moves the cluster toward the requested state instead of fighting it.

**One node at a time, stop at the first failure.** This keeps the blast radius small and the rollback reasoning simple.

**Documented success is trusted.** After a `201` from POST or a `200` from DELETE the client does not read the node back.

**Ambiguous mutation outcomes are reconciled via GET.** A timeout, a connection error or a 5xx after POST or DELETE does not say whether the node applied the change. The client reads the node and lets the observed state decide. If the state is already the requested one, the write happened and the answer was lost. A `400` from POST and a `404` from DELETE are handled the same way, because the brief says a `400` may mean the group already exists and does not say what a `404` on DELETE means.

**A mutation is repeated only with permission.** POST or DELETE is sent again only when all three hold: the previous answer was ambiguous, GET shows the node still in its old state, and attempts remain. Three attempts in total, with 0.1 s and then 0.2 s between them. A `400`, a `404` or any status outside the contract never leads to a second attempt: after the GET it is either already done or a hard failure.

**Rollback is best-effort compensating rollback.** On failure the client walks the nodes it touched in reverse order and drives each one back to the state observed before the operation, using the same rules as the forward path. It never deletes a group that existed before a create, never recreates a group that was absent before a delete, and never touches a node it did not reach. A node whose last verified state is already its old state needs no call at all.

**Unresolved state is surfaced.** If a node cannot be driven back, or its state cannot be verified, the error says so per node. The client never claims more than it verified.

## Assumptions

Where the brief is silent, this is what I chose.

- A host is a bare hostname, optionally with a port, or a root `http://` / `https://` URL. Bare hostnames get `http://`. A trailing slash is tolerated; a path, query or fragment is rejected. An empty host list and duplicate hosts are rejected before any call.
- A group id is any string with at least one non-whitespace character. It is sent unchanged in request bodies and percent-encoded in the GET path. Further naming rules belong to the server.
- Only the documented success codes count as success. Any other status, including other 2xx codes, is checked with GET and never retried.
- A GET answering `200` with a missing or different `groupId` is a protocol failure: `UNKNOWN`, without retry. A node that answers wrongly does not get better by being asked again.
- Every request has an explicit timeout, default 5 seconds. Backoff is fixed exponential without jitter.
- Nothing else changes the same group while an operation runs. The API offers no concurrency control, so the client cannot detect a concurrent writer.
- The client keeps its state in memory. A crash in the middle of an operation can leave the cluster partially changed; running the command again is the recovery path.

## Guarantees and limitations

The client guarantees that it never repeats a mutation blindly, never destroys state it did not create, and never reports success it did not verify. It does not provide an atomic transaction across nodes: the nodes are independent and the API has no prepare or commit step, so rollback is compensation after the fact and can itself fail. When that happens the error names the node and its last known state, and the cluster may be left inconsistent on purpose rather than papered over.

## Tests and quality checks

```
pytest
ruff check .
ruff format --check .
```

The tests use `httpx.MockTransport`, so nothing touches the network, and the retry policy takes an injected sleep function, so nothing waits. Every failure-path test asserts the exact sequence of HTTP calls, which is how "no blind retry" and "rollback touches only my nodes" are proven rather than assumed.

## What I would add for production

- Jitter on the backoff and separate connect and read timeouts, once more than one client shares the cluster.
- Bounded parallelism across nodes if latency matters more than the simplicity of sequential rollback.
- A durable record of in-flight operations, so a crashed run can be resumed instead of rerun.
- Structured logging and metrics around retries and rollbacks.
