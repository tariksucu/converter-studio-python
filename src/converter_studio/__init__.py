"""The official Python client for the Converter Studio API (https://converter.studio).

>>> from converter_studio import ConverterStudio
>>> with ConverterStudio() as client:  # the key comes from CONVERTER_STUDIO_API_KEY
...     conversion = client.convert("photo.heic", "jpg")
...     client.download(conversion, "out/")

Documentation: https://converter.studio/developers
"""

from ._async_client import AsyncConverterStudio
from ._base import API_KEY_ENV, BASE_URL_ENV, DEFAULT_BASE_URL, OPTION_TYPES, Source
from ._client import ConverterStudio
from ._errors import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfirmationRequiredError,
    ConflictError,
    ConversionError,
    ConversionFailedError,
    ConversionTimeoutError,
    ConverterStudioError,
    FileTooLargeError,
    GoneError,
    InsufficientCreditsError,
    MissingApiKeyError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    UnprocessableError,
)
from ._models import (
    FINISHED_STATUSES,
    SETTLED_STATUSES,
    Account,
    Allowance,
    Conversion,
    ConversionStatus,
    ErrorInfo,
    Format,
    Upload,
    Wallet,
)
from ._version import __version__

__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "FINISHED_STATUSES",
    "OPTION_TYPES",
    "SETTLED_STATUSES",
    "APIConnectionError",
    "APIError",
    "APITimeoutError",
    "Account",
    "Allowance",
    "AsyncConverterStudio",
    "AuthenticationError",
    "BadRequestError",
    "ConfirmationRequiredError",
    "ConflictError",
    "Conversion",
    "ConversionError",
    "ConversionFailedError",
    "ConversionStatus",
    "ConversionTimeoutError",
    "ConverterStudio",
    "ConverterStudioError",
    "ErrorInfo",
    "FileTooLargeError",
    "Format",
    "GoneError",
    "InsufficientCreditsError",
    "MissingApiKeyError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "ServerError",
    "Source",
    "UnprocessableError",
    "Upload",
    "Wallet",
    "__version__",
]
