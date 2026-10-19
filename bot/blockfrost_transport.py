"""SDK-compatible reads with explicit, local connect/read timeouts over one pooled session."""

import requests
from blockfrost import BlockFrostApi
from blockfrost.utils import list_request_wrapper, request_wrapper
from requests.adapters import HTTPAdapter

REQUEST_TIMEOUT = (3.05, 10)  # Connect timeout, read-idle timeout (seconds).
POOL_SIZE = 8  # Keep-alive connections kept open; also sizes the Blockfrost read executor.


def _build_session() -> requests.Session:
    """One keep-alive session shared by the read workers, so each block does not pay fresh TLS handshakes."""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=POOL_SIZE)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_session = _build_session()


class TimeoutBlockFrostApi(BlockFrostApi):
    """Keep SDK authentication/decoding while bounding our five HTTP read methods."""

    def _get(self, path: str, params: dict | None = None):
        return _session.get(
            url=f"{self.url}{path}", headers=self.default_headers, params=params, timeout=REQUEST_TIMEOUT
        )

    @request_wrapper
    def block(self, hash_or_number: str, **kwargs):
        return self._get(f"/blocks/{hash_or_number}")

    @list_request_wrapper
    def block_transactions(self, hash_or_number: str, **kwargs):
        return self._get(f"/blocks/{hash_or_number}/txs", self.query_parameters(kwargs))

    @request_wrapper
    def transaction(self, hash: str, **kwargs):
        return self._get(f"/txs/{hash}")

    @request_wrapper
    def asset(self, asset: str, **kwargs):
        return self._get(f"/assets/{asset}")

    @list_request_wrapper
    def asset_history(self, asset: str, **kwargs):
        return self._get(f"/assets/{asset}/history", self.query_parameters(kwargs))
