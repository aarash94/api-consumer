"""Command-line interface: the thin layer between the shell and ClusterClient."""

import argparse
import os
import sys

from api_consumer.client import ClusterClient, ClusterOperationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api-consumer",
        description="Create or delete a group on every node of a cluster, rolling back on failure.",
    )
    parser.add_argument("operation", choices=("create", "delete"), help="what to do with the group")
    parser.add_argument("group_id", metavar="GROUP_ID", help="the group to create or delete")
    parser.add_argument(
        "--hosts",
        default=os.environ.get("CLUSTER_HOSTS"),
        help="comma-separated node hosts, e.g. node1.example.com,node2.example.com "
        "(default: the CLUSTER_HOSTS environment variable)",
    )
    parser.add_argument(
        "--timeout", type=float, default=5.0, help="per-request timeout in seconds (default: 5)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.hosts:
        parser.error("--hosts is required when CLUSTER_HOSTS is not set")
    hosts = [host.strip() for host in args.hosts.split(",") if host.strip()]
    try:
        with ClusterClient(hosts, timeout=args.timeout) as client:
            operate = client.create_group if args.operation == "create" else client.delete_group
            result = operate(args.group_id)
    except ValueError as exc:
        parser.error(str(exc))
    except ClusterOperationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    changed = ", ".join(result.changed) or "none"
    skipped = ", ".join(result.skipped) or "none"
    print(f"{result.operation} {result.group_id!r} done: changed {changed}; skipped {skipped}")
    return 0
