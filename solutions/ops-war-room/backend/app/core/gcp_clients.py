"""Thread-safe lazy GCP client pool for BigQuery, Vertex AI, named Firestore DB, and Cloud Workflows."""

from __future__ import annotations

import asyncio
import functools
import threading
from typing import Any, Callable, TypeVar

from app.core.config import Settings

T = TypeVar("T")


async def run_blocking(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run blocking GCP SDK calls in the default executor without blocking the ASGI event loop."""
    loop = asyncio.get_running_loop()
    bound = functools.partial(fn, *args, **kwargs)
    return await loop.run_in_executor(None, bound)


class GCPClientPool:
    """Lazily initializes and caches GCP clients with RLock safety (zero import-time I/O)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._bq_client: Any = None
        self._genai_clients: dict[str, Any] = {}
        self._firestore_client: Any = None
        self._workflows_client: Any = None
        self._storage_client: Any = None
        self._dlp_client: Any = None

    @property
    def project_id(self) -> str:
        pid = self._settings.gcp_project_id.strip()
        if not pid:
            raise RuntimeError("GCP_PROJECT_ID is not configured; Live Cloud mode unavailable.")
        return pid

    def bigquery(self) -> Any:
        with self._lock:
            if self._bq_client is None:
                from google.cloud import bigquery

                self._bq_client = bigquery.Client(
                    project=self.project_id,
                    location=self._settings.bq_location,
                )
            return self._bq_client

    def genai(self, *, location: str | None = None) -> Any:
        loc = location or self._settings.gcp_region
        with self._lock:
            if loc not in self._genai_clients:
                from google import genai

                self._genai_clients[loc] = genai.Client(
                    vertexai=True,
                    project=self.project_id,
                    location=loc,
                )
            return self._genai_clients[loc]

    def firestore(self) -> Any:
        """Connect strictly to the dedicated named Native-mode Firestore database (acc04-ops-war-room)."""
        with self._lock:
            if self._firestore_client is None:
                from google.cloud import firestore

                self._firestore_client = firestore.Client(
                    project=self.project_id,
                    database=self._settings.firestore_database,
                )
            return self._firestore_client

    def workflows_executions(self) -> Any:
        with self._lock:
            if self._workflows_client is None:
                from google.cloud.workflows import executions_v1

                self._workflows_client = executions_v1.ExecutionsClient()
            return self._workflows_client

    def storage(self) -> Any:
        with self._lock:
            if self._storage_client is None:
                from google.cloud import storage

                self._storage_client = storage.Client(project=self.project_id)
            return self._storage_client

    def dlp(self) -> Any:
        with self._lock:
            if self._dlp_client is None:
                from google.cloud import dlp_v2

                self._dlp_client = dlp_v2.DlpServiceClient()
            return self._dlp_client
