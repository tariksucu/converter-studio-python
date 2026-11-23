"""The exceptions this package raises. All of them derive from :class:`ConverterStudioError`.

``str(error)`` is always a sentence meant for people: for API errors it is the API's own message.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import Conversion, ParamValue


class ConverterStudioError(Exception):
    """Base class of every error raised by this package."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class MissingApiKeyError(ConverterStudioError):
    """No API key was given, and ``CONVERTER_STUDIO_API_KEY`` is not set."""


class APIConnectionError(ConverterStudioError):
    """The API could not be reached, or the connection broke (after the retries)."""


class APITimeoutError(APIConnectionError):
    """The API did not answer in time (after the retries)."""


class APIError(ConverterStudioError):
    """The API answered with an error.

    ``code`` is the API's stable error code (``invalidApiKey``, ``notEnoughCredits``,
    ``unsupportedConversion``...), ``message`` its English sentence for people and ``params`` the
    values in it. ``code`` is ``None`` only when the answer did not come from the API itself (a
    proxy's error page).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None,
        params: Mapping[str, ParamValue] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.params: Mapping[str, ParamValue] = dict(params or {})
        self.headers: Mapping[str, str] = dict(headers or {})

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code}, code={self.code!r}, "
            f"message={self.message!r})"
        )


class BadRequestError(APIError):
    """400: the request was not right (``invalidRequest``, ``invalidUrl``, ``invalidCallbackUrl``,
    ``invalidIdempotencyKey``...)."""


class AuthenticationError(APIError):
    """401: the API key does not work (wrong or deleted)."""


class InsufficientCreditsError(APIError):
    """402: not enough credits for this conversion (``params``: ``needed``, ``balance``)."""


class PermissionDeniedError(APIError):
    """403: the plan does not include this."""


class NotFoundError(APIError):
    """404: no such conversion or upload on this account."""


class ConflictError(APIError):
    """409: the request conflicts with what is there (``idempotencyKeyReused``, an upload that has
    not received its file yet...)."""


class GoneError(APIError):
    """410: the result has been deleted (``fileExpired``)."""


class FileTooLargeError(APIError):
    """413: the file is larger than the plan allows (``params``: ``maxSize``)."""


class UnprocessableError(APIError):
    """422: the file cannot be converted this way (``unsupportedConversion``...)."""


class RateLimitError(APIError):
    """429: too many requests; ``retry_after`` says how many seconds to wait, when known."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ServerError(APIError):
    """5xx: the service is busy or failed (``serverBusy``); trying again later may work."""


class ConversionError(ConverterStudioError):
    """A conversion ended without a result, or did not end in time. ``conversion`` is its last
    known state."""

    def __init__(self, message: str, conversion: Conversion) -> None:
        super().__init__(message)
        self.conversion = conversion


class ConversionFailedError(ConversionError):
    """The conversion failed, expired or was canceled; nothing was charged for a failure.

    ``code`` and ``message`` are the API's (``conversionFailed``, ``noText``, ``wrongPassword``,
    ``remoteNotFound``...).
    """

    def __init__(self, conversion: Conversion) -> None:
        error = conversion.error
        if error is not None:
            code, message = error.code, error.message
        elif conversion.status == "expired":
            code, message = "fileExpired", "The result was deleted after its time ran out."
        elif conversion.status == "canceled":
            code, message = "canceled", "The conversion was canceled; nothing was charged."
        else:
            code, message = "conversionFailed", conversion.next or "The conversion failed."
        super().__init__(message, conversion)
        self.code = code
        self.params: Mapping[str, ParamValue] = dict(error.params) if error else {}


class ConfirmationRequiredError(ConversionError):
    """The conversion costs more than ``max_credits``: it waits for
    :meth:`~converter_studio.ConverterStudio.confirm` and nothing has been charged."""

    def __init__(self, conversion: Conversion) -> None:
        credits = conversion.credits
        limit = conversion.max_credits or 0
        super().__init__(
            f"This conversion costs {credits} credits, more than the {limit} allowed to go ahead "
            "on their own. Confirm it to start, or leave it: nothing has been charged.",
            conversion,
        )
        self.credits = credits
        self.max_credits = conversion.max_credits


class ConversionTimeoutError(ConversionError):
    """The conversion did not finish within the time given. It may still finish: wait for it
    again, or look it up later with :meth:`~converter_studio.ConverterStudio.get_conversion`."""
