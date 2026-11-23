"""What the sync and the async client share: settings, request bodies, errors, retries, files."""

from __future__ import annotations

import email.utils
import io
import json
import os
import platform
import random
import re
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TypeAlias
from urllib.parse import quote, urlsplit

import httpx

from ._errors import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfirmationRequiredError,
    ConflictError,
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
from ._models import FINISHED_STATUSES, Conversion, ParamValue
from ._version import __version__

DEFAULT_BASE_URL = "https://converter.studio/api"
API_KEY_ENV = "CONVERTER_STUDIO_API_KEY"
BASE_URL_ENV = "CONVERTER_STUDIO_BASE_URL"
CLIENT_HEADER = "X-Converter-Studio-Client"
CLIENT_NAME = "python"
#: Seconds for one request to connect, send or receive a chunk.
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
#: How long :meth:`wait` waits for a conversion by default, in seconds.
DEFAULT_WAIT_TIMEOUT = 600.0
#: The API holds a status request up to this many seconds until the conversion settles.
LONG_POLL_SECONDS = 25
#: One request's body at most (the CDN in front of the API stops at 100 MB).
DEFAULT_PART_BYTES = 95 * 1024 * 1024
#: Bytes read from a file at a time while it is sent or saved.
CHUNK_BYTES = 1024 * 1024
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
#: A file object that cannot seek is copied to memory up to this size, then to a temporary file.
SPOOL_BYTES = 8 * 1024 * 1024

Source: TypeAlias = str | os.PathLike[str] | bytes | bytearray | memoryview | IO[bytes]
"""What can be converted: a path, the file's bytes, a binary file object, or (``str`` starting
with ``http://`` or ``https://``) a link to a file."""

_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")

# The client header's rules (packages/shared/src/clients.ts in the API): a name of lower-case
# letters, digits and hyphens, then a version; longer values are ignored by the server.
_CLIENT_NAME = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*/"
    r"(?P<version>[0-9]{1,6}\.[0-9]{1,6}(?:\.[0-9]{1,6})?"
    r"(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)"
)
_MAX_CLIENT_HEADER = 64
_MAX_CLIENT_VERSION = 32

# The site's English messages for the codes the download route sends without one.
_FALLBACK_MESSAGES = {
    "fileExpired": "This file has already been deleted. Please convert it again.",
    "notFound": "We couldn't find that conversion.",
    "tooManyRequests": "You're going a bit fast. Please wait a moment and try again.",
    "serverBusy": "Our servers are busy right now. Please try again in a few minutes.",
    "invalidRequest": "Something about that request wasn't right.",
    "invalidApiKey": (
        "This API key does not work. Create one on your account page at converter.studio."
    ),
}

_ERRORS_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: InsufficientCreditsError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    410: GoneError,
    413: FileTooLargeError,
    422: UnprocessableError,
    429: RateLimitError,
}


def user_agent(client: str | None = None) -> str:
    """The User-Agent: a package built on this client (``client``) comes first."""
    own = f"converter-studio-python/{__version__} Python/{platform.python_version()}"
    return f"{client} {own}" if client else own


def check_client_name(client_name: str | None) -> str | None:
    """``client_name`` if it can be sent in the client header: ``<name>/<version>``, the name in
    lower-case letters, digits and hyphens (the server's rule, as it stores it)."""
    if client_name is None:
        return None
    match = _CLIENT_NAME.fullmatch(client_name) if isinstance(client_name, str) else None
    if (
        match is None
        or len(client_name) > _MAX_CLIENT_HEADER
        or len(match["version"]) > _MAX_CLIENT_VERSION
    ):
        raise ValueError(
            "client_name must be <name>/<version>, such as 'langchain/1.0.0': lower-case letters, "
            "digits and hyphens, a slash and a version (1.2 or 1.2.3, then -beta.1 or +build.5 at "
            f"most), {_MAX_CLIENT_HEADER} characters in all; not {client_name!r}"
        )
    return client_name


def client_headers(client_name: str | None = None) -> dict[str, str]:
    """Sent on every request: which client asks (statistics only), and its version.
    ``client_name``: a package built on this client, named instead (checked by
    :func:`check_client_name`)."""
    return {
        CLIENT_HEADER: client_name or f"{CLIENT_NAME}/{__version__}",
        "User-Agent": user_agent(client_name),
    }


def resolve_api_key(api_key: str | None) -> str | None:
    key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
    key = (key or "").strip()
    return key or None


def resolve_base_url(base_url: str | None) -> str:
    url = (base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"base_url must be an http:// or https:// address, not {url!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("base_url must not hold a query, a fragment, a user name or a password")
    return url


def require_key(api_key: str | None) -> str:
    if api_key is None:
        raise MissingApiKeyError(
            f"No API key: pass api_key=... or set {API_KEY_ENV}. "
            "Create a key on your account page at https://converter.studio/account#api-keys."
        )
    return api_key


def check_idempotency_key(key: str) -> str:
    if not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError("idempotency_key must be 1 to 255 visible ASCII characters")
    return key


def new_idempotency_key() -> str:
    return f"cvs-py-{uuid.uuid4().hex}"


def conversion_body(
    *,
    to: str,
    url: str | None,
    upload_id: str | None,
    options: Mapping[str, object] | None,
    max_credits: int | None,
    callback_url: str | None,
) -> dict[str, Any]:
    if (url is None) == (upload_id is None):
        raise ValueError("Give either url or upload_id")
    if not to:
        raise ValueError("to (the target format) is needed")
    body: dict[str, Any] = {"to": to.lower().lstrip(".")}
    if url is not None:
        body["url"] = url
    if upload_id is not None:
        body["uploadId"] = upload_id
    if options:
        body["options"] = dict(options)
    if max_credits is not None:
        if isinstance(max_credits, bool) or max_credits < 0:
            raise ValueError("max_credits must be a whole number, 0 or more")
        body["maxCredits"] = max_credits
    if callback_url is not None:
        body["callbackUrl"] = callback_url
    return body


def _params(value: object) -> dict[str, ParamValue]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, str | int | float) and not isinstance(item, bool)
    }


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` in seconds (a number or an HTTP date), when the answer has one."""
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def error_from_response(response: httpx.Response) -> APIError:
    """The API's error (``{"error": {"code", "message", "params"}}``; the download route's
    ``{"error": "code"}``), or a plain one for answers that did not come from the API."""
    status = response.status_code
    code: str | None = None
    message: str | None = None
    params: dict[str, ParamValue] = {}
    try:
        body = json.loads(response.content) if response.content else None
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            code = error.get("code") if isinstance(error.get("code"), str) else None
            message = error.get("message") if isinstance(error.get("message"), str) else None
            params = _params(error.get("params"))
        elif isinstance(error, str):
            code = error
            params = _params(body.get("params"))
    if message is None:
        message = _FALLBACK_MESSAGES.get(code or "") or (
            f"The Converter Studio API answered {status} {response.reason_phrase}".rstrip() + "."
        )
    headers = {key.lower(): value for key, value in response.headers.items()}
    kwargs: dict[str, Any] = {
        "status_code": status,
        "code": code,
        "params": params,
        "headers": headers,
    }
    if status == 429:
        return RateLimitError(message, retry_after=retry_after_seconds(headers), **kwargs)
    error_class = _ERRORS_BY_STATUS.get(status, ServerError if status >= 500 else APIError)
    return error_class(message, **kwargs)


def connection_error(exc: httpx.TransportError, url: httpx.URL | str) -> APIConnectionError:
    """A transport error in words, naming only the host (links carry tokens)."""
    host = httpx.URL(url).host if isinstance(url, str) else url.host
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError(f"The request to {host} timed out.")
    reason = str(exc) or type(exc).__name__
    return APIConnectionError(f"Could not reach {host}: {reason}")


def is_retryable_exception(exc: httpx.TransportError) -> bool:
    return isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError)


def retry_delay(attempt: int, retry_after: float | None) -> float:
    """Seconds before retry ``attempt`` (0-based): the server's ``Retry-After`` (at most a
    minute), or an exponential back-off with jitter."""
    if retry_after is not None:
        return min(retry_after, 60.0)
    base: float = min(0.5 * 2**attempt, 8.0)
    return base * (0.5 + random.random() / 2)  # noqa: S311 (jitter, not a secret)


def poll_backoff(previous: float) -> float:
    """The next pause between status requests that came back early: 1 s, doubling to 10 s."""
    return 1.0 if previous <= 0 else min(previous * 2, 10.0)


def is_url(source: object) -> bool:
    return isinstance(source, str) and source.lower().startswith(("http://", "https://"))


def api_url(base_url: str, path: str) -> str:
    return f"{base_url}{path}"


def same_origin(url: str, base_url: str) -> bool:
    """Whether ``url`` is under the API's address (where the API key may go)."""
    return url == base_url or url.startswith(base_url + "/")


_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")


def safe_file_name(name: str | None, fallback: str) -> str:
    """A name to save a result under: its last part only, without control characters."""
    candidate = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = _UNSAFE_NAME.sub("_", candidate).strip().strip(".")
    return candidate or fallback


def free_path(path: Path, overwrite: bool) -> Path:
    """``path``, or with ``-1``, ``-2``... before its extension when a file is already there."""
    if overwrite or not path.exists():
        return path
    suffixes = "".join(path.suffixes[-2:]) if path.name.endswith(".tar.gz") else path.suffix
    stem = path.name[: len(path.name) - len(suffixes)] if suffixes else path.name
    for n in range(1, 10_000):
        candidate = path.with_name(f"{stem}-{n}{suffixes}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No free name for {path}")


def target_path(destination: str | os.PathLike[str] | None, output_name: str) -> Path:
    """Where a result goes: into a folder (the current one when none is given) under its own
    name, or to the exact path given."""
    if destination is None:
        return Path.cwd() / output_name
    path = Path(destination)
    if path.is_dir() or str(destination).endswith(("/", os.sep)):
        return path / output_name
    return path


def temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.part")


@dataclass
class FileSource:
    """A file to upload, readable from any offset (so a part can be sent again)."""

    name: str
    size: int
    _path: Path | None = None
    _data: bytes | None = None
    _stream: IO[bytes] | None = None
    _start: int = 0
    _owned: bool = False

    def open_at(self, offset: int) -> IO[bytes]:
        """A reader at ``offset``; close it with :meth:`release`."""
        if self._path is not None:
            reader: IO[bytes] = self._path.open("rb")
            reader.seek(offset)
            return reader
        if self._data is not None:
            reader = io.BytesIO(self._data)
            reader.seek(offset)
            return reader
        if self._stream is None:
            raise ValueError("A file source needs a path, bytes or a stream")
        self._stream.seek(self._start + offset)
        return self._stream

    def release(self, reader: IO[bytes]) -> None:
        if reader is not self._stream:
            reader.close()

    def close(self) -> None:
        if self._owned and self._stream is not None:
            self._stream.close()


def file_source(source: Source, file_name: str | None) -> FileSource:
    """What to upload, with its name (the API reads the format from the bytes and the name)."""
    if isinstance(source, str | os.PathLike):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"No such file: {path}")
        return FileSource(name=file_name or path.name, size=path.stat().st_size, _path=path)
    if isinstance(source, bytes | bytearray | memoryview):
        if not file_name:
            raise ValueError("file_name is needed with bytes (for example 'photo.heic')")
        data = bytes(source)
        return FileSource(name=file_name, size=len(data), _data=data)
    own_name = getattr(source, "name", None)
    name = file_name or (Path(own_name).name if isinstance(own_name, str) else "")
    if not name:
        raise ValueError("file_name is needed with a file object that has no name")
    if _seekable(source):
        start = source.tell()
        end = source.seek(0, io.SEEK_END)
        source.seek(start)
        return FileSource(name=name, size=end - start, _stream=source, _start=start)
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)  # noqa: SIM115 (closed by close())
    while chunk := source.read(CHUNK_BYTES):
        spool.write(chunk)
    size = spool.tell()
    return FileSource(name=name, size=size, _stream=spool, _owned=True)


def _seekable(stream: IO[bytes]) -> bool:
    try:
        return bool(stream.seekable())
    except (AttributeError, ValueError, OSError):
        return False


def iter_part(reader: IO[bytes], length: int) -> Iterator[bytes]:
    """``length`` bytes of ``reader``, a chunk at a time."""
    left = length
    while left > 0:
        chunk = reader.read(min(CHUNK_BYTES, left))
        if not chunk:
            raise ValueError("The file got shorter while it was being sent")
        left -= len(chunk)
        yield chunk


def part_ranges(size: int, part_bytes: int) -> list[tuple[int, int]]:
    """``(offset, length)`` of each part; one empty part for an empty file."""
    if size <= part_bytes:
        return [(0, size)]
    return [(offset, min(part_bytes, size - offset)) for offset in range(0, size, part_bytes)]


def upload_headers(length: int, offset: int | None) -> dict[str, str]:
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(length)}
    if offset is not None:
        headers["Upload-Offset"] = str(offset)
    return headers


def is_settled(conversion: Conversion) -> bool:
    """``True`` once the result is ready, ``False`` while it is on its way; raises when it ended
    without one or waits for a go-ahead."""
    if conversion.status == "done":
        return True
    if conversion.status in FINISHED_STATUSES:
        raise ConversionFailedError(conversion)
    if conversion.status == "quoted":
        raise ConfirmationRequiredError(conversion)
    return False


def timeout_error(conversion: Conversion, timeout: float) -> ConversionTimeoutError:
    return ConversionTimeoutError(
        f"The conversion did not finish within {timeout:g} seconds (it is {conversion.status}). "
        f"It may still finish: look it up later by its id, {conversion.id}.",
        conversion,
    )


def not_ready_error(conversion: Conversion) -> ConverterStudioError:
    if conversion.status in FINISHED_STATUSES:
        return ConversionFailedError(conversion)
    if conversion.status == "quoted":
        return ConfirmationRequiredError(conversion)
    return ConverterStudioError(
        f"The conversion is not done yet (it is {conversion.status}); wait for it first."
    )


def long_poll_timeout(timeout: httpx.Timeout, wait: int) -> httpx.Timeout:
    """The client's timeouts, with room to read an answer the API holds for ``wait`` seconds."""
    read = max(timeout.read or 0.0, wait + 30.0)
    return httpx.Timeout(connect=timeout.connect, read=read, write=timeout.write, pool=timeout.pool)


def poll_seconds(remaining: float) -> int:
    """How long the API may hold the next status request: up to 25 s, within what is left."""
    return max(1, min(LONG_POLL_SECONDS, int(remaining)))


def conversion_path(conversion_id: str) -> str:
    if not conversion_id:
        raise ValueError("A conversion id is needed")
    return f"/v1/conversions/{quote(conversion_id, safe='')}"


#: Options whose values are not text, by type. Every other option is text, also those that
#: look like numbers (``height=720`` is the text ``"720"``).
OPTION_TYPES: dict[str, type] = {
    **dict.fromkeys(
        (
            "mute",
            "copyVideo",
            "normalize",
            "fade",
            "copyAudio",
            "keepMetadata",
            "keepLocation",
            "lossless",
            "flip",
            "pdfA",
            "sheetsOnePage",
            "ocr",
            "header",
            "tocPage",
            "hiddenSlides",
        ),
        bool,
    ),
    **dict.fromkeys(("maxSize", "resizeWidth", "resizeHeight", "maxKb", "sheet"), int),
    **dict.fromkeys(("trimStart", "trimEnd", "start", "subtitleShift"), float),
}

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def parse_option(key: str, value: str) -> object:
    """One option's value from text: a boolean, a number or text, by the option's type."""
    kind = OPTION_TYPES.get(key)
    text = value.strip()
    if kind is bool:
        if text.lower() in _TRUE:
            return True
        if text.lower() in _FALSE:
            return False
        raise ValueError(f"{key} takes true or false, not {value!r}")
    if kind is int:
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{key} takes a whole number, not {value!r}") from None
    if kind is float:
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{key} takes a number, not {value!r}") from None
        return int(number) if number.is_integer() else number
    return value
