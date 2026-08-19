"""Actionable extract errors (ONTA-553). Routes map these to 503 / 422 / 502."""


class DltNotInstalled(RuntimeError):
    """Optional extra missing. Routes map this to 503, not 500."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "dlt is not installed. Install the optional extra: "
                "pip install 'infona-client[dlt]'"
            )
        )


class DltSecretMissing(ValueError):
    """BYOK credential absent. Routes map this to 422, not 500."""


class DltExtractError(RuntimeError):
    """Upstream extract failed (auth, HTTP, SQL). Routes map this to 502."""
