class WorkbenchError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class LeaseLost(WorkbenchError):
    def __init__(self):
        super().__init__("Task cancelled or worker lease expired", 409)


class InvalidDecision(WorkbenchError):
    """A read-only model result failed validation, not a transport failure."""
