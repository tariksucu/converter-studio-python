"""The asynchronous client (asyncio or Trio, through httpx and AnyIO)."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import IO, Any

import anyio
import anyio.to_thread
import httpx

from . import _base
from ._base import Source
from ._client import _conversion_id, _json
from ._errors import APIConnectionError, NotFoundError
from ._models import SETTLED_STATUSES, Account, Conversion, Format, Upload


class AsyncConverterStudio:
    """The asynchronous twin of :class:`ConverterStudio`: the same arguments (``client_name``
    included; ``http_client`` is an ``httpx.AsyncClient``), the same methods, to be awaited, and
    :meth:`iter_download` as an async iterator.

    >>> async with AsyncConverterStudio() as client:
    ...     conversion = await client.convert("photo.heic", "jpg")
    ...     await client.download(conversion, "out/")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout = _base.DEFAULT_TIMEOUT,
        max_retries: int = _base.DEFAULT_MAX_RETRIES,
        http_client: httpx.AsyncClient | None = None,
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
        self._http = http_client or httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        self._sleep: Callable[[float], Awaitable[None]] = anyio.sleep
        self._clock: Callable[[], float] = time.monotonic

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    async def aclose(self) -> None:
        """Closes the connections (not those of an ``http_client`` you passed)."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncConverterStudio:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    # The API

    async def formats(self, from_format: str | None = None) -> list[Format]:
        """See :meth:`ConverterStudio.formats`."""
        params = {"from": from_format.lower().lstrip(".")} if from_format else None
        response = await self._request(
            "GET", "/v1/formats", params=params, auth=self._api_key is not None
        )
        return [Format.from_api(item) for item in _json(response).get("formats", [])]

    async def account(self) -> Account:
        """See :meth:`ConverterStudio.account`."""
        return Account.from_api(_json(await self._request("GET", "/v1/account")))

    async def create_upload(self, file_name: str, size: int | None = None) -> Upload:
        """See :meth:`ConverterStudio.create_upload`."""
        body: dict[str, Any] = {"fileName": file_name}
        if size is not None:
            body["size"] = size
        return Upload.from_api(_json(await self._request("POST", "/v1/uploads", json=body)))

    async def upload(
        self, file: Source, *, file_name: str | None = None, part_size: int | None = None
    ) -> Upload:
        """See :meth:`ConverterStudio.upload`."""
        source = await anyio.to_thread.run_sync(_base.file_source, file, file_name)
        try:
            slot = await self.create_upload(source.name, source.size)
            limit = slot.part_bytes or _base.DEFAULT_PART_BYTES
            part_bytes = min(part_size, limit) if part_size else limit
            ranges = _base.part_ranges(source.size, part_bytes)
            for offset, length in ranges:
                await self._send_part(
                    slot, source, offset, length, offset if len(ranges) > 1 else None
                )
            return slot
        finally:
            source.close()

    async def create_conversion(
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
        """See :meth:`ConverterStudio.create_conversion`."""
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
        response = await self._request(
            "POST", "/v1/conversions", json=body, headers={"Idempotency-Key": key}
        )
        replayed = response.headers.get("idempotent-replayed", "").lower() == "true"
        return Conversion.from_api(_json(response), replayed=replayed)

    async def get_conversion(
        self, conversion: Conversion | str, *, wait: int | None = None
    ) -> Conversion:
        """See :meth:`ConverterStudio.get_conversion`."""
        path = _base.conversion_path(_conversion_id(conversion))
        if not wait:
            return Conversion.from_api(_json(await self._request("GET", path)))
        seconds = max(1, min(int(wait), 60))
        response = await self._request(
            "GET",
            path,
            params={"wait": str(seconds)},
            timeout=_base.long_poll_timeout(self._timeout, seconds),
        )
        return Conversion.from_api(_json(response))

    async def confirm(self, conversion: Conversion | str) -> Conversion:
        """See :meth:`ConverterStudio.confirm`."""
        path = _base.conversion_path(_conversion_id(conversion)) + "/confirm"
        return Conversion.from_api(_json(await self._request("POST", path)))

    # Waiting and results

    async def wait(
        self, conversion: Conversion | str, *, timeout: float = _base.DEFAULT_WAIT_TIMEOUT
    ) -> Conversion:
        """See :meth:`ConverterStudio.wait`."""
        deadline = self._clock() + timeout
        current = (
            conversion
            if isinstance(conversion, Conversion)
            else await self.get_conversion(conversion)
        )
        pause = 0.0
        while not _base.is_settled(current):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _base.timeout_error(current, timeout)
            seconds = _base.poll_seconds(remaining)
            started = self._clock()
            current = await self.get_conversion(current.id, wait=seconds)
            if current.status in SETTLED_STATUSES:
                continue
            if self._clock() - started < seconds / 2:
                pause = _base.poll_backoff(pause)
                await self._sleep(min(pause, max(0.0, deadline - self._clock())))
            else:
                pause = 0.0
        return current

    async def convert(
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
        """See :meth:`ConverterStudio.convert`."""
        common: dict[str, Any] = {
            "options": options,
            "max_credits": max_credits,
            "callback_url": callback_url,
            "idempotency_key": idempotency_key,
        }
        if isinstance(source, str) and _base.is_url(source):
            created = await self.create_conversion(to, url=source, **common)
        else:
            upload = await self.upload(source, file_name=file_name, part_size=part_size)
            created = await self.create_conversion(to, upload_id=upload.id, **common)
        return await self.wait(created, timeout=timeout) if wait else created

    async def iter_download(
        self, conversion: Conversion | str, *, chunk_size: int = _base.CHUNK_BYTES
    ) -> AsyncIterator[bytes]:
        """See :meth:`ConverterStudio.iter_download`."""
        done = await self._ready(conversion)
        url = done.download_url or ""
        try:
            async with self._http.stream(
                "GET", url, headers=_base.client_headers(self._client_name), follow_redirects=True
            ) as response:
                if not response.is_success:
                    await response.aread()
                    raise _base.error_from_response(response)
                async for chunk in response.aiter_bytes(chunk_size):
                    yield chunk
        except httpx.TransportError as exc:
            raise _base.connection_error(exc, url) from exc

    async def download_bytes(self, conversion: Conversion | str) -> bytes:
        """See :meth:`ConverterStudio.download_bytes`."""
        return b"".join([chunk async for chunk in self.iter_download(conversion)])

    async def download(
        self,
        conversion: Conversion | str,
        destination: str | os.PathLike[str] | None = None,
        *,
        overwrite: bool = False,
    ) -> Path:
        """See :meth:`ConverterStudio.download`."""
        done = await self._ready(conversion)
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
                async with await anyio.open_file(temporary, "wb") as out:
                    async for chunk in self.iter_download(done):
                        await out.write(chunk)
                temporary.replace(path)
                return path
            except APIConnectionError:
                temporary.unlink(missing_ok=True)
                if attempt >= self.max_retries:
                    raise
                await self._sleep(_base.retry_delay(attempt, None))
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    # Internals

    async def _ready(self, conversion: Conversion | str) -> Conversion:
        current = (
            conversion
            if isinstance(conversion, Conversion)
            else await self.get_conversion(conversion)
        )
        if current.status != "done" or not current.download_url:
            raise _base.not_ready_error(current)
        return current

    async def _request(
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
                response = await self._http.request(
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
                await self._sleep(_base.retry_delay(attempt, None))
                continue
            if response.is_success:
                return response
            if last or response.status_code not in _base.RETRY_STATUSES:
                raise _base.error_from_response(response)
            await self._sleep(
                _base.retry_delay(attempt, _base.retry_after_seconds(response.headers))
            )
        raise AssertionError("unreachable")  # pragma: no cover

    async def _send_part(
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
            reader = await anyio.to_thread.run_sync(source.open_at, offset)
            try:
                response = await self._http.request(
                    "PUT",
                    slot.upload_url,
                    content=_aiter_part(reader, length),
                    headers=headers,
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                if last or not _base.is_retryable_exception(exc):
                    raise _base.connection_error(exc, slot.upload_url) from exc
                await self._sleep(_base.retry_delay(attempt, None))
                continue
            finally:
                source.release(reader)
            if response.is_success:
                return
            error = _base.error_from_response(response)
            if attempt > 0 and isinstance(error, NotFoundError):
                return
            if last or response.status_code not in _base.RETRY_STATUSES:
                raise error
            await self._sleep(
                _base.retry_delay(attempt, _base.retry_after_seconds(response.headers))
            )


async def _aiter_part(reader: IO[bytes], length: int) -> AsyncIterator[bytes]:
    left = length
    while left > 0:
        chunk = await anyio.to_thread.run_sync(reader.read, min(_base.CHUNK_BYTES, left))
        if not chunk:
            raise ValueError("The file got shorter while it was being sent")
        left -= len(chunk)
        yield chunk
