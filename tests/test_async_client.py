from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from converter_studio import (
    AsyncConverterStudio,
    AuthenticationError,
    ConfirmationRequiredError,
    ConversionFailedError,
    ConversionTimeoutError,
    GoneError,
    MissingApiKeyError,
    __version__,
)

from .conftest import Clock
from .fake_api import BASE, KEY, FakeAPI, error_response

pytestmark = pytest.mark.anyio
CONVERSIONS = "/api/v1/conversions"


@pytest.fixture
async def aclient(api: FakeAPI, clock: Clock) -> AsyncIterator[AsyncConverterStudio]:
    async with AsyncConverterStudio(
        api_key=KEY, base_url=BASE, http_client=api.async_client()
    ) as c:
        c._sleep = clock.asleep
        c._clock = clock
        yield c


def photo(tmp_path: Path, size: int = 300) -> Path:
    path = tmp_path / "IMG_1.HEIC"
    path.write_bytes(b"p" * size)
    return path


async def test_convert_and_download(
    aclient: AsyncConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    conversion = await aclient.convert(photo(tmp_path), "jpg", options={"quality": "small"})
    assert conversion.is_done
    path = await aclient.download(conversion, tmp_path)
    assert path == tmp_path / "IMG_1.jpg"
    assert path.read_bytes() == b"RESULT"
    for request in api.requests:
        assert request.headers["x-converter-studio-client"] == f"python/{__version__}"
    created = json.loads(api.of("POST", CONVERSIONS)[0].content)
    assert created == {"to": "jpg", "uploadId": "up00000001", "options": {"quality": "small"}}


async def test_a_package_built_on_the_client_names_itself_on_every_request(
    api: FakeAPI, clock: Clock, tmp_path: Path
) -> None:
    async with AsyncConverterStudio(
        api_key=KEY, base_url=BASE, http_client=api.async_client(), client_name="haystack/1.0.0"
    ) as named:
        named._sleep, named._clock = clock.asleep, clock
        await named.download_bytes(await named.convert(photo(tmp_path), "jpg"))
    assert {r.method for r in api.requests} == {"POST", "PUT", "GET"}
    for request in api.requests:
        assert request.headers["x-converter-studio-client"] == "haystack/1.0.0"
        assert request.headers["user-agent"].startswith(
            f"haystack/1.0.0 converter-studio-python/{__version__} Python/"
        )
    with pytest.raises(ValueError, match="client_name must be <name>/<version>"):
        AsyncConverterStudio(api_key=KEY, client_name="Haystack")


async def test_parts_and_retries(
    aclient: AsyncConverterStudio, api: FakeAPI, clock: Clock, tmp_path: Path
) -> None:
    path = photo(tmp_path, 2100)
    api.push("/api/v1/uploads", error_response(503, "serverBusy", "Busy."))
    api.push("/api/v1/uploads/up00000001", httpx.WriteError("cut"))
    upload = await aclient.upload(path, part_size=1000)
    puts = api.of("PUT", f"/api/v1/uploads/{upload.id}")
    assert [p.headers["upload-offset"] for p in puts] == ["0", "0", "1000", "2000"]
    assert api.uploads[upload.id]["data"] == path.read_bytes()
    assert len(clock.sleeps) == 2


async def test_bytes_and_links(aclient: AsyncConverterStudio, api: FakeAPI) -> None:
    await aclient.convert(b"abc", "txt", file_name="notes.md")
    linked = await aclient.convert("https://e/song.flac", "mp3", wait=False)
    assert linked.status == "fetching"
    bodies = [json.loads(r.content) for r in api.of("POST", CONVERSIONS)]
    assert bodies[1] == {"to": "mp3", "url": "https://e/song.flac"}


async def test_formats_and_account(api: FakeAPI, aclient: AsyncConverterStudio) -> None:
    assert [f.id for f in await aclient.formats("mov")] == ["mov"]
    assert (await aclient.account()).email == "dev@example.com"
    async with AsyncConverterStudio(base_url=BASE, http_client=api.async_client()) as anonymous:
        assert len(await anonymous.formats()) == 2
        with pytest.raises(MissingApiKeyError):
            await anonymous.account()


async def test_errors(api: FakeAPI, aclient: AsyncConverterStudio) -> None:
    async with AsyncConverterStudio(
        api_key="cvs_live_wrong", base_url=BASE, http_client=api.async_client()
    ) as wrong:
        with pytest.raises(AuthenticationError):
            await wrong.account()
    api.statuses = ["failed"]
    api.error = {"code": "noText", "message": "No text was found."}
    with pytest.raises(ConversionFailedError) as failed:
        await aclient.convert("https://e/scan.pdf", "txt")
    assert failed.value.code == "noText"
    api.statuses, api.credits = ["done"], 50
    with pytest.raises(ConfirmationRequiredError) as quoted:
        await aclient.convert("https://e/film.mkv", "mp4")
    done = await aclient.wait(await aclient.confirm(quoted.value.conversion))
    assert done.is_done


async def test_wait_times_out(aclient: AsyncConverterStudio, api: FakeAPI, clock: Clock) -> None:
    api.statuses = ["processing"]
    created = await aclient.convert("https://e/a.mov", "mp4", wait=False)
    with pytest.raises(ConversionTimeoutError):
        await aclient.wait(created.id, timeout=5)
    assert sum(clock.sleeps) <= 5


async def test_streaming_and_failed_downloads(
    aclient: AsyncConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    api.output = b"y" * 2_500_000
    conversion = await aclient.convert("https://e/a.heic", "jpg")
    chunks = [c async for c in aclient.iter_download(conversion, chunk_size=1_000_000)]
    assert [len(c) for c in chunks] == [1_000_000, 1_000_000, 500_000]
    assert await aclient.download_bytes(conversion.id) == api.output
    api.push(
        f"/api/jobs/{conversion.id}/download", httpx.Response(410, json={"error": "fileExpired"})
    )
    with pytest.raises(GoneError):
        await aclient.download(conversion, tmp_path)
    assert list(tmp_path.iterdir()) == []
    api.push(f"/api/jobs/{conversion.id}/download", httpx.ReadError("cut"))
    path = await aclient.download(conversion, tmp_path)
    assert path.stat().st_size == 2_500_000
