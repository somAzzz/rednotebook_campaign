class DomainError(Exception):
    """Stable, content-free operational error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
