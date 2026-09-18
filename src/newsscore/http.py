"""One place to build the shared HTTP client."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:  # only annotations need httpx here; fetching imports it for real
    import httpx



def make_client(timeout: float = 20.0) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` with sane defaults for polling news APIs.

    Connection-level failures are retried twice by the transport; HTTP error
    statuses are left to each source to interpret.
    """
    import httpx  # deferred: ~50 ms, and only a run that fetches ever pays it

    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": f"newsscore/{__version__}", "Accept": "application/json, */*"},
        follow_redirects=True,
        transport=httpx.AsyncHTTPTransport(retries=2),
    )
