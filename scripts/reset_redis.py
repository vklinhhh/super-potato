#!/usr/bin/env python3
"""
reset_redis.py

Flushes ALL data from a dev/staging Redis environment -- used to clean up test or
simulated data between test rounds without touching any other environment.

Deliberately refuses to run against 'prod': environments are fully separate
Memorystore clusters (see vbio.clients.redis_connection's docstring for why), so prod
should never accumulate test data in the first place. If you genuinely need to wipe
prod, do it manually and deliberately -- not via a script someone could run by habit.

Usage:
    python scripts/reset_redis.py --env dev
    python scripts/reset_redis.py --env staging --config default.json
    python scripts/reset_redis.py --env dev --yes   # skip the confirmation prompt (e.g. CI)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vbio.clients.redis_connection import build_redis_client_for_env  # noqa: E402

BLOCKED_ENVS = {"prod", "production"}


def reset_environment(env: str, config_path: str = "default.json") -> None:
    client = build_redis_client_for_env(env=env, config_path=config_path)

    try:
        from redis.cluster import RedisCluster
        if isinstance(client, RedisCluster):
            # A cluster has multiple shards -- flushall() on the client alone only
            # targets one node unless explicitly told to hit every primary.
            client.flushall(target_nodes=RedisCluster.PRIMARIES)
            return
    except ImportError:
        pass

    client.flushall()


def _main():
    parser = argparse.ArgumentParser(description="Flush all data in a dev/staging Redis environment.")
    parser.add_argument("--env", required=True, help="Environment name from default.json (e.g. dev, staging).")
    parser.add_argument("--config", default="default.json", help="Path to config json.")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    args = parser.parse_args()

    if args.env.lower() in BLOCKED_ENVS:
        print(
            f"Refusing to run against '{args.env}'. This script only targets dev/staging "
            f"by design -- if you genuinely need to reset production data, do it manually "
            f"and deliberately, not through this tool."
        )
        sys.exit(1)

    if not args.yes:
        typed = input(
            f"This will PERMANENTLY delete ALL data in the '{args.env}' Redis environment.\n"
            f"Type the environment name to confirm: "
        )
        if typed.strip() != args.env:
            print("Confirmation did not match. Aborted -- nothing was deleted.")
            sys.exit(1)

    reset_environment(args.env, config_path=args.config)
    print(f"'{args.env}' Redis environment flushed.")


if __name__ == "__main__":
    _main()
