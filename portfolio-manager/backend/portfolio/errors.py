class PortfolioPersistenceError(Exception):
    """Base error for expected persistence failures."""


class MigrationError(PortfolioPersistenceError):
    pass


class SnapshotNotFoundError(PortfolioPersistenceError):
    pass


class InactiveSnapshotError(PortfolioPersistenceError):
    pass


class DuplicateImportError(PortfolioPersistenceError):
    def __init__(self, message: str, snapshot_id: str):
        super().__init__(message)
        self.snapshot_id = snapshot_id


class BundleError(PortfolioPersistenceError):
    pass


class KiteError(Exception):
    """Base error for safe, user-facing Kite integration failures."""


class KiteNotConfiguredError(KiteError):
    pass


class KiteAuthenticationError(KiteError):
    pass


class KiteCallbackError(KiteError):
    pass


class KiteHoldingsError(KiteError):
    pass


class KiteUpstreamError(KiteError):
    def __init__(self, message: str, *, status_code: int = 502, token_expired: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.token_expired = token_expired
