from fastapi import HTTPException


class NotFoundError(Exception):
    """Mapped to 404 by an exception handler registered with add_exception_handler."""


class QuotaError(Exception):
    """Mapped to 429 by an @app.exception_handler decorator."""


class TeapotError(HTTPException):
    def __init__(self):
        super().__init__(status_code=418, detail="short and stout")
