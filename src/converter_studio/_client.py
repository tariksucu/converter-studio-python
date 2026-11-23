"""The synchronous client."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from . import _base
from ._base import Source
from ._errors import APIConnectionError, NotFoundError
from ._models import SETTLED_STATUSES, Account, Conversion, Format, Upload


class ConverterStudio:
    """A client for the Converter Studio API (https://converter.studio/developers).

    >>> from converter_studio import ConverterStudio
    >>> client = ConverterStudio()  # reads CONVERTER_STUDIO_API_KEY
    >>> conversion = client.convert("photo.heic", "jpg")
    >>> client.download(conversion, "out/")

    ``api_key``: the account's API key; ``CONVERTER_STUDIO_API_KEY`` when not given. Listing
    formats needs none. ``base_url``: the API's address, ``CONVERTER_STUDIO_BASE_URL`` or
    ``https://converter.studio/api``. ``timeout``: seconds for each network step of a request.
    ``max_retries``: how often a request is sent again after a network error, a 408, 429 or 5xx
    answer (with a back-off; conversions are created with an ``Idempotency-Key``, so a retry is
    never charged twice). ``http_client``: your own ``httpx.Client`` (proxies, certificates);
    it is not closed by :meth:`close`. ``client_name``: for a package built on this client, its
    own ``"<name>/<version>"`` (``"langchain/1.0.0"``: lower-case letters, digits and hyphens,
    then the version), sent in the ``X-Converter-Studio-Client`` header instead of
    ``python/<version>`` and put in front of the User-Agent; the API counts usage by it.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = _base.DEFAULT_TIMEOUT,
        max_retries: int = _base.DEFAULT_MAX_RETRIES,
        http_client: httpx.Client | None = None,
        client_name: str | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be 0 or more")
        self._client_name = _base.check_client_name(client_name)
        self._api_key = _base.resolve_api_key(api_key)
        self.base_url = _base.resolve_base_url(base_url)
        self.max_retries = max_retries
        self._timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=self._timeout, follow_redirects=False)
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    def close(self) -> None:
        """Closes the connections (not those of an ``http_client`` you passed)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> ConverterStudio:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # The API

    def formats(self, from_format: str | None = None) -> list[Format]:
        """The formats the API reads and what each converts to right now (no key needed).
        ``from_format`` keeps only that one."""
        params = {"from": from_format.lower().lstrip(".")} if from_format else None
        response = self._request(
            "GET", "/v1/formats", params=params, auth=self._api_key is not None
        )
        return [Format.from_api(item) for item in _json(response).get("formats", [])]

    def account(self) -> Account:
        """The key's account: plan, credits left, limits."""
        return Account.from_api(_json(self._request("GET", "/v1/account")))

    def create_upload(self, file_name: str, size: int | None = None) -> Upload:
        """Opens an upload slot (``POST /v1/uploads``). :meth:`upload` does this and sends the
        file."""
        body: dict[str, Any] = {"fileName": file_name}
        if size is not None:
            body["size"] = size
        return Upload.from_api(_json(self._request("POST", "/v1/uploads", json=body)))

    def upload(
        self, file: Source, *, file_name: str | None = None, part_size: int | None = None
    ) -> Upload:
        """Sends a file (a path, bytes or a binary file object) and returns its upload; convert
        it with :meth:`create_conversion` (``upload_id=upload.id``). Files larger than one
        request carries (95 MB) go in parts; ``part_size`` makes the parts smaller."""
        source = _base.file_source(file, file_name)
        try:
            slot = self.create_upload(source.name, source.size)
            limit = slot.part_bytes or _base.DEFAULT_PART_BYTES
            part_bytes = min(part_size, limit) if part_size else limit
            ranges = _base.part_ranges(source.size, part_bytes)
            for offset, length in ranges:
                self._send_part(slot, source, offset, length, offset if len(ranges) > 1 else None)
            return slot
        finally:
            source.close()

    def create_conversion(
        self,
        to: str,
        *,
        url: str | None = None,
        upload_id: str | None = None,
        options: Mapping[str, object] | None = None,
        max_credits: int | None = None,
        callback_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> Conversion:
        """Starts converting a link's file (``url``) or an upload (``upload_id``) to ``to``.

        ``options``: the conversion's settings (``{"quality": "high"}``; see the formats page).
        ``max_credits``: it goes ahead on its own up to this price (the API's default is 20; 0
        only prices it). ``callback_url``: an https:// address the API POSTs to when it waits for
        a go-ahead and when it ends. ``idempotency_key``: a repeat of the same request with the
        same key returns the first conversion (one is made up when not given, and kept for this
        call's retries).
        """
        body = _base.conversion_body(
            to=to,
            url=url,
            upload_id=upload_id,
            options=options,
            max_credits=max_credits,
            callback_url=callback_url,
        )
        key = (
            _base.check_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else _base.new_idempotency_key()
        )
        response = self._request(
            "POST", "/v1/conversions", json=body, headers={"Idempotency-Key": key}
        )
        replayed = response.headers.get("idempotent-replayed", "").lower() == "true"
        return Conversion.from_api(_json(response), replayed=replayed)

    def get_conversion(
        self, conversion: Conversion | str, *, wait: int | None = None
    ) -> Conversion:
        """A conversion's state now; with ``wait`` (1 to 60 seconds), the API holds the answer
        until the conversion is done, failed or waits for a go-ahead, or the time is up."""
        path = _base.conversion_path(_conversion_id(conversion))
        if not wait:
            return Conversion.from_api(_json(self._request("GET", path)))
        seconds = max(1, min(int(wait), 60))
        response = self._request(
            "GET",
            path,
            params={"wait": str(seconds)},
            timeout=_base.long_poll_timeout(self._timeout, seconds),
        )
        return Conversion.from_api(_json(response))

    def confirm(self, conversion: Conversion | str) -> Conversion:
        """Goes ahead with a conversion that costs more than its ``max_credits`` (``quoted``)."""
        path = _base.conversion_path(_conversion_id(conversion)) + "/confirm"
        return Conversion.from_api(_json(self._request("POST", path)))

    # Waiting and results

    def wait(
        self, conversion: Conversion | str, *, timeout: float = _base.DEFAULT_WAIT_TIMEOUT
    ) -> Conversion:
        """Waits until the conversion is done and returns it.

        Raises :class:`ConversionFailedError` when it fails, expires or is canceled,
        :class:`ConfirmationRequiredError` when it costs more than ``max_credits`` (see
        :meth:`confirm`) and :class:`ConversionTimeoutError` after ``timeout`` seconds (it may
        still finish). The API holds each status request until something changes; answers that
        come back early are followed by a growing pause (1 s, doubling to 10 s).
        """
        deadline = self._clock() + timeout
        current = (
            conversion if isinstance(conversion, Conversion) else self.get_conversion(conversion)
        )
        pause = 0.0
        while not _base.is_settled(current):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _base.timeout_error(current, timeout)
            seconds = _base.poll_seconds(remaining)
            started = self._clock()
            current = self.get_conversion(current.id, wait=seconds)
            if current.status in SETTLED_STATUSES:
                continue
            if self._clock() - started < seconds / 2:
                pause = _base.poll_backoff(pause)
                self._sleep(min(pause, max(0.0, deadline - self._clock())))
            else:
                pause = 0.0
        return current

    def convert(
        self,
        source: Source,
        to: str,
        *,
        file_name: str | None = None,
        options: Mapping[str, object] | None = None,
        max_credits: int | None = None,
        callback_url: str | None = None,
        idempotency_key: str | None = None,
        wait: bool = True,
        timeout: float = _base.DEFAULT_WAIT_TIMEOUT,
        part_size: int | None = None,
    ) -> Conversion:
        """Converts a file to ``to`` and, unless ``wait=False``, waits until it is done.

        ``source``: a path, the file's bytes (with ``file_name``), a binary file object, or a
        link (a ``str`` starting with ``http://`` or ``https://``; pass a :class:`pathlib.Path`
        for a local file named like that). The other arguments are those of
        :meth:`create_conversion` and :meth:`wait`. Save the result with :meth:`download`.
        """
        common: dict[str, Any] = {
            "options": options,
            "max_credits": max_credits,
            "callback_url": callback_url,
            "idempotency_key": idempotency_key,
        }
        if isinstance(source, str) and _base.is_url(source):
            created = self.create_conversion(to, url=source, **common)
        else:
            upload = self.upload(source, file_name=file_name, part_size=part_size)
            created = self.create_conversion(to, upload_id=upload.id, **common)
        return self.wait(created, timeout=timeout) if wait else created

    def iter_download(
        self, conversion: Conversion | str, *, chunk_size: int = _base.CHUNK_BYTES
    ) -> Iterator[bytes]:
        """The result's bytes as they arrive (nothing is kept in memory)."""
        done = self._ready(conversion)
        url = done.download_url or ""
        try:
            with self._http.stream(
                "GET", url, headers=_base.client_headers(self._client_name), follow_redirects=True
            ) as response:
                if not response.is_success:
                    response.read()
                    raise _base.error_from_response(response)
                yield from response.iter_bytes(chunk_size)
        except httpx.TransportError as exc:
            raise _base.connection_error(exc, url) from exc

    def download_bytes(self, conversion: Conversion | str) -> bytes:
        """The result, in memory."""
        return b"".join(self.iter_download(conversion))

    def download(
        self,
        conversion: Conversion | str,
        destination: str | os.PathLike[str] | None = None,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Saves the result and returns where.

        ``destination``: a folder that exists or a path ending in ``/`` (the result keeps its
        name; the current folder when none is given), or a file path. In a folder, an existing
        file is never replaced unless ``overwrite``: the result gets ``-1``, ``-2``... before its
        extension. An existing file path given exactly raises :class:`FileExistsError` unless
        ``overwrite``. The file appears only once complete.
        """
        done = self._ready(conversion)
        name = _base.safe_file_name(done.output_name, f"{done.id}.{done.to_format or 'bin'}")
        path = _base.target_path(destination, name)
        exact = destination is not None and path == Path(destination)
        if exact and path.exists() and not overwrite:
            raise FileExistsError(f"{path} exists; pass overwrite=True to replace it")
        path = _base.free_path(path, overwrite)
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(self.max_retries + 1):
            temporary = _base.temporary_path(path)
            try:
                with temporary.open("wb") as out:
                    for chunk in self.iter_download(done):
                        out.write(chunk)
                temporary.replace(path)
                return path
            except APIConnectionError:
                temporary.unlink(missing_ok=True)
                if attempt >= self.max_retries:
                    raise
                self._sleep(_base.retry_delay(attempt, None))
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    # Internals

    def _ready(self, conversion: Conversion | str) -> Conversion:
        current = (
            conversion if isinstance(conversion, Conversion) else self.get_conversion(conversion)
        )
        if current.status != "done" or not current.download_url:
            raise _base.not_ready_error(current)
        return current

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        url = _base.api_url(self.base_url, path)
        request_headers = {**_base.client_headers(self._client_name), **(headers or {})}
        if auth:
            request_headers["Authorization"] = f"Bearer {_base.require_key(self._api_key)}"
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            last = attempt + 1 >= attempts
            try:
                response = self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers=request_headers,
                    timeout=timeout if timeout is not None else self._timeout,
                )
            except httpx.TransportError as exc:
                if last or not _base.is_retryable_exception(exc):
                    raise _base.connection_error(exc, url) from exc
                self._sleep(_base.retry_delay(attempt, None))
                continue
            if response.is_success:
                return response
            if last or response.status_code not in _base.RETRY_STATUSES:
                raise _base.error_from_response(response)
            self._sleep(_base.retry_delay(attempt, _base.retry_after_seconds(response.headers)))
        raise AssertionError("unreachable")  # pragma: no cover

    def _send_part(
        self,
        slot: Upload,
        source: _base.FileSource,
        offset: int,
        length: int,
        header_offset: int | None,
    ) -> None:
        headers = {
            **_base.client_headers(self._client_name),
            **_base.upload_headers(length, header_offset),
        }
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            last = attempt + 1 >= attempts
            reader = source.open_at(offset)
            try:
                response = self._http.request(
                    "PUT",
                    slot.upload_url,
                    content=_base.iter_part(reader, length),
                    headers=headers,
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                if last or not _base.is_retryable_exception(exc):
                    raise _base.connection_error(exc, slot.upload_url) from exc
                self._sleep(_base.retry_delay(attempt, None))
                continue
            finally:
                source.release(reader)
            if response.is_success:
                return
            error = _base.error_from_response(response)
            # The slot is gone once its last part is in: an earlier attempt got there.
            if attempt > 0 and isinstance(error, NotFoundError):
                return
            if last or response.status_code not in _base.RETRY_STATUSES:
                raise error
            self._sleep(_base.retry_delay(attempt, _base.retry_after_seconds(response.headers)))


def _conversion_id(conversion: Conversion | str) -> str:
    return conversion.id if isinstance(conversion, Conversion) else conversion


def _json(response: httpx.Response) -> Mapping[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = None
    return data if isinstance(data, Mapping) else {}
