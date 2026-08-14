"""
iam_auth.py

Builds a redis-py CredentialProvider that authenticates to a Memorystore for Redis
Cluster instance using IAM auth, refreshing the access token as needed.

IMPORTANT -- which approach this uses, and why:
GCP's own sample code (see "Client library connection code samples" in the Memorystore
for Redis Cluster docs) uses google.cloud.iam_credentials_v1.IAMCredentialsClient to
IMPERSONATE a target service account and mint a token for it. That's the right tool
when the calling identity is DIFFERENT from the service account you want to
authenticate as (e.g. a human running code locally, or one service minting tokens for
another). On Cloud Run, the runtime already IS that service account -- there's no
impersonation to do. This module instead refreshes the running service's own
Application Default Credentials (ADC) directly via `google.auth`, which is simpler and
needs only the `roles/redis.dbConnectionUser` grant (no `roles/iam.serviceAccountTokenCreator`
self-impersonation grant required).

If you DO need to mint tokens for a different service account than the one your code
is running as, use GCP's impersonation-based sample instead:
https://cloud.google.com/memorystore/docs/cluster/client-library-connection

Usage:
    from vbio.clients.iam_auth import IAMCredentialProvider
    from redis.cluster import RedisCluster, ClusterNode

    client = RedisCluster(
        startup_nodes=[ClusterNode(DISCOVERY_IP, DISCOVERY_PORT)],
        credential_provider=IAMCredentialProvider(),
        ssl=True,
        ssl_ca_certs="./server-ca.pem",
        decode_responses=True,
    )
"""
from datetime import datetime, timezone
from typing import Tuple

import google.auth
import google.auth.transport.requests
import redis

# GCP access tokens default to a 1-hour lifetime. Refresh a bit before that so a
# connection attempt never races an about-to-expire token.
DEFAULT_REFRESH_MARGIN_SECONDS = 300

IAM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class IAMCredentialProvider(redis.CredentialProvider):
    """
    Supplies (username, password) to redis-py on every new connection, where the
    "password" is a live GCP IAM access token for the identity this code is running
    as (via Application Default Credentials) -- matching how Memorystore for Redis
    Cluster's IAM auth integrates with the Redis AUTH command.
    """

    def __init__(self, refresh_margin_seconds: int = DEFAULT_REFRESH_MARGIN_SECONDS):
        self._credentials, _ = google.auth.default(scopes=[IAM_SCOPE])
        self._refresh_margin_seconds = refresh_margin_seconds

    def _needs_refresh(self) -> bool:
        if not self._credentials.valid:
            return True
        if self._credentials.expiry is None:
            return False
        remaining = (self._credentials.expiry - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
        return remaining < self._refresh_margin_seconds

    def get_credentials(self) -> Tuple[str, str]:
        """Called by redis-py before authenticating each new connection."""
        if self._needs_refresh():
            self._credentials.refresh(google.auth.transport.requests.Request())
        # Username is intentionally empty -- the access token IS the credential,
        # matching the pattern in GCP's own Go/Java IAM-auth samples (Username: "").
        return "", self._credentials.token
