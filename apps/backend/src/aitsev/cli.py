"""Command-line entry points."""

from __future__ import annotations

import os

import uvicorn


def run_api() -> None:
    """Start the API server (``aitsev-api``)."""
    uvicorn.run(
        "aitsev.main:create_app",
        factory=True,
        host=os.environ.get("AITSEV_HOST", "0.0.0.0"),  # noqa: S104 - container default
        port=int(os.environ.get("PORT", os.environ.get("AITSEV_PORT", "8000"))),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
