"""
redis_connection.py

Builds a redis-py client (standalone or cluster, optionally TLS/IAM-auth) from the
"redis" config block for a given environment in default.json -- so every script or
service in this project connects to Redis the same way instead of each one
re-implementing connection setup (and each one accidentally drifting from the others).

Config shape (per environment, in default.json):
    "dev": {
        "redis": {
            "url": "redis://localhost:6379/0",
            "cluster": false,
            "tlsCaCert": null,
            "iamAuth": false
        }
    }

Environments are fully separate Memorystore clusters (dev / staging / prod), NOT a
single shared cluster split by key prefix or DB index -- Redis Cluster mode only
supports DB 0 (the SELECT command is disabled), so DB-index or prefix-based separation
isn't a safe option here. Keeping each environment as its own cluster instance means a
bug in application code can never accidentally write test data into prod, or vice versa.

Usage:
    from vbio.clients.redis_connection import build_redis_client_for_env

    client = build_redis_client_for_env(env="dev", config_path="default.json")
"""
import json
from pathlib import Path
from typing import Optional


class RedisEnvConfig:
    """Reads the 'redis' config block for one environment from a default.json-style file."""

    def __init__(self, config_path: str = "default.json", env: str = "dev"):
        self.config_path = Path(config_path)
        self.env = env
        raw = self._load()

        if env not in raw:
            raise KeyError(f"Environment '{env}' not found in {self.config_path}")
        redis_cfg = raw[env].get("redis")
        if not redis_cfg:
            raise KeyError(f"No 'redis' config found under '{env}' in {self.config_path}")

        self.url: str = redis_cfg["url"]
        self.cluster: bool = redis_cfg.get("cluster", False)
        self.tls_ca_cert: Optional[str] = redis_cfg.get("tlsCaCert")
        self.iam_auth: bool = redis_cfg.get("iamAuth", False)

    def _load(self):
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)


def build_redis_client(
    redis_url: str,
    use_cluster: bool = False,
    tls_ca_cert: Optional[str] = None,
    iam_auth: bool = False,
):
    """
    Builds either a standalone redis.Redis client (plain local Redis, e.g. dev on
    localhost) or a redis.cluster.RedisCluster client (required for GCP Memorystore
    for Redis Cluster -- a standalone client can't follow cluster hash-slot redirects
    and will fail against a cluster endpoint).
    """
    import redis

    ssl_kwargs = {}
    if tls_ca_cert:
        # Required if the target has in-transit encryption enabled.
        ssl_kwargs = {"ssl": True, "ssl_ca_certs": tls_ca_cert}

    credential_kwargs = {}
    if iam_auth:
        from vbio.clients.iam_auth import IAMCredentialProvider
        credential_kwargs = {"credential_provider": IAMCredentialProvider()}

    if use_cluster:
        from redis.cluster import RedisCluster
        return RedisCluster.from_url(redis_url, decode_responses=True, **ssl_kwargs, **credential_kwargs)

    return redis.Redis.from_url(redis_url, decode_responses=True, **ssl_kwargs, **credential_kwargs)


def build_redis_client_for_env(env: str = "dev", config_path: str = "default.json"):
    """Convenience: reads the 'redis' block for `env` from config and builds a client from it."""
    cfg = RedisEnvConfig(config_path=config_path, env=env)
    return build_redis_client(
        cfg.url, use_cluster=cfg.cluster, tls_ca_cert=cfg.tls_ca_cert, iam_auth=cfg.iam_auth,
    )
