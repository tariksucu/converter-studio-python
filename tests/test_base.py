from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from converter_studio import (
    Account,
    APIError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    Conversion,
    FileTooLargeError,
    Format,
    GoneError,
    InsufficientCreditsError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    UnprocessableError,
    Upload,
    _base,
)


def test_conversion_from_api_reads_every_field() -> None:
    conversion = Conversion.from_api(
        {
            "id": "abc12345",
            "status": "failed",
            "from": "heic",
            "to": "jpg",
            "inputName": "IMG.HEIC",
            "inputSize": 12375,
            "credits": 1,
            "maxCredits": 20,
            "progress": None,
            "outputName": None,
            "outputSize": None,
            "downloadUrl": None,
            "expiresAt": "2026-11-23T07:28:39.741Z",
            "error": {
                "code": "conversionFailed",
                "message": "Something went wrong.",
                "params": {"limit": 5, "flag": True, "name": "x"},
            },
            "next": "The conversion failed; no credits were charged for it.",
            "newField": 1,
        }
    )
    assert conversion.id == "abc12345"
    assert conversion.from_format == "heic"
    assert conversion.to_format == "jpg"
    assert conversion.input_size == 12375
    assert conversion.expires_at == datetime(2026, 11, 23, 7, 28, 39, 741000, tzinfo=UTC)
    assert conversion.error is not None
    assert conversion.error.code == "conversionFailed"
    assert dict(conversion.error.params) == {"limit": 5, "name": "x"}
    assert conversion.raw["newField"] == 1
    assert conversion.is_finished
    assert not conversion.is_done


def test_conversion_tolerates_missing_and_wrong_fields() -> None:
    conversion = Conversion.from_api({"id": "x", "status": "done", "credits": "1", "progress": 1})
    assert conversion.credits is None
    assert conversion.progress == 1.0
    assert conversion.error is None
    assert conversion.expires_at is None
    assert Conversion.from_api(None).status == "failed"


def test_secret_links_stay_out_of_repr() -> None:
    conversion = Conversion.from_api(
        {"id": "x", "status": "done", "downloadUrl": "https://h/jobs/x/download?token=SECRET"}
    )
    upload = Upload.from_api({"id": "u", "uploadUrl": "https://h/v1/uploads/u?token=SECRET"})
    assert "SECRET" not in repr(conversion)
    assert "SECRET" not in repr(upload)
    assert "curl" not in upload.raw


def test_format_and_account() -> None:
    fmt = Format.from_api(
        {"id": "heic", "name": "HEIC", "category": "image", "extensions": ["heic"], "to": ["jpg"]}
    )
    assert fmt.targets == ("jpg",)
    account = Account.from_api(
        {
            "email": "a@example.com",
            "plan": "free",
            "allApps": False,
            "credits": 13,
            "allowance": {"used": 0, "limit": 13, "per": "day"},
            "packs": 0,
            "renewsAt": "2026-11-24T00:00:00.000Z",
            "maxFileBytes": 104857600,
            "maxMediaSeconds": 600,
            "wallet": None,
        }
    )
    assert account.allowance is not None
    assert account.allowance.per == "day"
    assert account.wallet is None
    assert not account.all_apps


def test_api_key_from_argument_or_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _base.resolve_api_key(None) is None
    monkeypatch.setenv("CONVERTER_STUDIO_API_KEY", "  cvs_live_env \n")
    assert _base.resolve_api_key(None) == "cvs_live_env"
    assert _base.resolve_api_key("cvs_live_arg") == "cvs_live_arg"
    assert _base.resolve_api_key("  ") is None


def test_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _base.resolve_base_url(None) == "https://converter.studio/api"
    monkeypatch.setenv("CONVERTER_STUDIO_BASE_URL", "http://localhost:8080/api/")
    assert _base.resolve_base_url(None) == "http://localhost:8080/api"
    assert _base.resolve_base_url("https://example.com/x/") == "https://example.com/x"
    for wrong in (
        "ftp://example.com",
        "example.com",
        "https://u:p@example.com",
        "https://e.com?a=1",
    ):
        with pytest.raises(ValueError, match="base_url"):
            _base.resolve_base_url(wrong)


def test_conversion_body() -> None:
    body = _base.conversion_body(
        to=".JPG",
        url=None,
        upload_id="up000001",
        options={"quality": "high"},
        max_credits=0,
        callback_url="https://example.com/cb",
    )
    assert body == {
        "to": "jpg",
        "uploadId": "up000001",
        "options": {"quality": "high"},
        "maxCredits": 0,
        "callbackUrl": "https://example.com/cb",
    }
    with pytest.raises(ValueError, match="either"):
        _base.conversion_body(
            to="jpg", url="https://e/x", upload_id="u", options=None, max_credits=None,
            callback_url=None,
        )  # fmt: skip
    with pytest.raises(ValueError, match="max_credits"):
        _base.conversion_body(
            to="jpg", url="https://e/x", upload_id=None, options=None, max_credits=-1,
            callback_url=None,
        )  # fmt: skip


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "çok", "tab\t"])
def test_idempotency_key_rules(key: str) -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        _base.check_idempotency_key(key)


def test_idempotency_keys_are_new_and_valid() -> None:
    first, second = _base.new_idempotency_key(), _base.new_idempotency_key()
    assert first != second
    assert _base.check_idempotency_key(first) == first


@pytest.mark.parametrize(
    ("status", "error_class"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (402, InsufficientCreditsError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (410, GoneError),
        (413, FileTooLargeError),
        (422, UnprocessableError),
        (429, RateLimitError),
        (500, ServerError),
        (503, ServerError),
        (418, APIError),
    ],
)
def test_errors_by_status(status: int, error_class: type[APIError]) -> None:
    response = httpx.Response(
        status, json={"error": {"code": "someCode", "message": "Said by the API.", "params": {}}}
    )
    error = _base.error_from_response(response)
    assert type(error) is error_class
    assert error.status_code == status
    assert error.code == "someCode"
    assert str(error) == "Said by the API."


def test_error_with_params() -> None:
    response = httpx.Response(
        402,
        json={
            "error": {
                "code": "notEnoughCredits",
                "message": "This needs 3 credits and you have 1.",
                "params": {"needed": 3, "balance": 1},
            }
        },
    )
    error = _base.error_from_response(response)
    assert isinstance(error, InsufficientCreditsError)
    assert dict(error.params) == {"needed": 3, "balance": 1}
    assert "notEnoughCredits" in repr(error)


def test_site_style_and_foreign_errors() -> None:
    gone = _base.error_from_response(httpx.Response(410, json={"error": "fileExpired"}))
    assert isinstance(gone, GoneError)
    assert gone.code == "fileExpired"
    assert "deleted" in gone.message
    proxy = _base.error_from_response(httpx.Response(502, text="<html>Bad gateway</html>"))
    assert isinstance(proxy, ServerError)
    assert proxy.code is None
    assert "502" in proxy.message


def test_retry_after() -> None:
    limited = _base.error_from_response(
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "x"}})
    )
    assert isinstance(limited, RateLimitError)
    assert limited.retry_after == 7.0
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    seconds = _base.retry_after_seconds({"retry-after": later})
    assert seconds is not None
    assert 25 <= seconds <= 31
    assert _base.retry_after_seconds({"retry-after": "soon"}) is None
    assert _base.retry_after_seconds({}) is None


def test_retry_delay() -> None:
    assert _base.retry_delay(0, 120.0) == 60.0
    assert _base.retry_delay(3, 2.0) == 2.0
    for attempt in range(6):
        delay = _base.retry_delay(attempt, None)
        ceiling = min(0.5 * 2**attempt, 8.0)
        assert ceiling / 2 <= delay <= ceiling


def test_poll_backoff() -> None:
    pauses = [0.0]
    for _ in range(6):
        pauses.append(_base.poll_backoff(pauses[-1]))
    assert pauses[1:] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert _base.poll_seconds(500) == 25
    assert _base.poll_seconds(7.9) == 7
    assert _base.poll_seconds(0.2) == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
        ("a\x00b\nc.pdf", "a_b_c.pdf"),
        ("..", "fallback.bin"),
        ("", "fallback.bin"),
        (None, "fallback.bin"),
    ],
)
def test_safe_file_name(name: str | None, expected: str) -> None:
    assert _base.safe_file_name(name, "fallback.bin") == expected


def test_free_path(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"1")
    (tmp_path / "a-1.jpg").write_bytes(b"1")
    (tmp_path / "b.tar.gz").write_bytes(b"1")
    assert _base.free_path(tmp_path / "a.jpg", overwrite=False) == tmp_path / "a-2.jpg"
    assert _base.free_path(tmp_path / "a.jpg", overwrite=True) == tmp_path / "a.jpg"
    assert _base.free_path(tmp_path / "b.tar.gz", overwrite=False) == tmp_path / "b-1.tar.gz"
    assert _base.free_path(tmp_path / "new.png", overwrite=False) == tmp_path / "new.png"


def test_parse_option() -> None:
    assert _base.parse_option("height", "720") == "720"
    assert _base.parse_option("quality", "high") == "high"
    assert _base.parse_option("maxSize", "25") == 25
    assert _base.parse_option("trimStart", "1.5") == 1.5
    assert _base.parse_option("trimStart", "2") == 2
    assert _base.parse_option("keepMetadata", "Yes") is True
    assert _base.parse_option("mute", "off") is False
    assert _base.parse_option("newOption", "7") == "7"
    with pytest.raises(ValueError, match="true or false"):
        _base.parse_option("ocr", "maybe")
    with pytest.raises(ValueError, match="whole number"):
        _base.parse_option("maxKb", "big")
    with pytest.raises(ValueError, match="a number"):
        _base.parse_option("start", "x")


def test_part_ranges() -> None:
    assert _base.part_ranges(0, 10) == [(0, 0)]
    assert _base.part_ranges(10, 10) == [(0, 10)]
    assert _base.part_ranges(25, 10) == [(0, 10), (10, 10), (20, 5)]


def test_file_sources(tmp_path: Path) -> None:
    path = tmp_path / "IMG.HEIC"
    path.write_bytes(b"0123456789")
    from_path = _base.file_source(path, None)
    assert (from_path.name, from_path.size) == ("IMG.HEIC", 10)
    reader = from_path.open_at(4)
    assert reader.read() == b"456789"
    from_path.release(reader)

    with pytest.raises(ValueError, match="file_name"):
        _base.file_source(b"abc", None)
    from_bytes = _base.file_source(b"abc", "a.txt")
    assert from_bytes.size == 3

    stream = io.BytesIO(b"skip|payload")
    stream.seek(5)
    from_stream = _base.file_source(stream, "p.bin")
    assert from_stream.size == 7
    assert from_stream.open_at(2).read() == b"yload"

    with pytest.raises(FileNotFoundError):
        _base.file_source(tmp_path / "missing.heic", None)
    with path.open("rb") as handle:
        assert _base.file_source(handle, None).name == "IMG.HEIC"


class _Pipe(io.RawIOBase):
    """A stream that cannot seek, like a pipe."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        chunk = self._data.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)


def test_a_stream_that_cannot_seek_is_spooled() -> None:
    source = _base.file_source(io.BufferedReader(_Pipe(b"x" * 1000)), "pipe.bin")
    assert source.size == 1000
    assert source.open_at(990).read() == b"x" * 10
    source.close()
