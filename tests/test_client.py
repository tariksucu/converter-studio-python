from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from converter_studio import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfirmationRequiredError,
    ConflictError,
    Conversion,
    ConversionFailedError,
    ConversionTimeoutError,
    ConverterStudio,
    ConverterStudioError,
    GoneError,
    MissingApiKeyError,
    ServerError,
    __version__,
)

from .conftest import Clock
from .fake_api import BASE, KEY, FakeAPI, error_response

CONVERSIONS = "/api/v1/conversions"


def heic(tmp_path: Path, size: int = 64) -> Path:
    path = tmp_path / "IMG_1234.HEIC"
    path.write_bytes(bytes(range(256)) * (size // 256) + b"h" * (size % 256))
    return path


def test_every_request_names_the_client_and_only_api_requests_carry_the_key(
    client: ConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    conversion = client.convert(heic(tmp_path), "jpg")
    client.download_bytes(conversion)
    methods = [(r.method, r.url.path) for r in api.requests]
    assert ("PUT", "/api/v1/uploads/up00000001") in methods
    assert ("GET", "/api/jobs/up00000001/download") in methods
    for request in api.requests:
        assert request.headers["x-converter-studio-client"] == f"python/{__version__}"
        assert request.headers["user-agent"].startswith(f"converter-studio-python/{__version__}")
        carries_key = "authorization" in request.headers
        is_api = request.url.path.startswith("/api/v1/") and request.method != "PUT"
        assert carries_key == is_api, request.url.path


def test_a_package_built_on_the_client_names_itself_on_every_request(
    api: FakeAPI, clock: Clock, tmp_path: Path
) -> None:
    with ConverterStudio(
        api_key=KEY, base_url=BASE, http_client=api.client(), client_name="langchain/1.0.0"
    ) as named:
        named._sleep, named._clock = clock.sleep, clock
        named.download_bytes(named.convert(heic(tmp_path), "jpg"))
    assert {r.method for r in api.requests} == {"POST", "PUT", "GET"}
    for request in api.requests:
        assert request.headers["x-converter-studio-client"] == "langchain/1.0.0"
        assert request.headers["user-agent"].startswith(
            f"langchain/1.0.0 converter-studio-python/{__version__} Python/"
        )


@pytest.mark.parametrize(
    "name",
    [
        "langchain-converter-studio/1.0.0",
        "django/2.1",
        "haystack/1.0.0-beta.1+build.5",
        "x" * 58 + "/1.0.0",
    ],
)
def test_client_names_the_server_stores(name: str, api: FakeAPI) -> None:
    with ConverterStudio(base_url=BASE, http_client=api.client(), client_name=name) as named:
        named.formats()
    assert api.requests[0].headers["x-converter-studio-client"] == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "langchain",
        "langchain/",
        "LangChain/1.0.0",
        "lang chain/1.0.0",
        "-langchain/1.0.0",
        "langchain-/1.0.0",
        "lang_chain/1.0.0",
        "langchain/v1.0.0",
        "langchain/1",
        "langchain/1.0.0 extra",
        "langchain/1.0.0\r\nX-Other: 1",
        "langchain/\u0661.\u0660",  # digits, but not ASCII ones
        "x" * 59 + "/1.0.0",
        "langchain/1.0.0-" + "b" * 30,
    ],
)
def test_client_names_the_server_would_not_store_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="client_name must be <name>/<version>"):
        ConverterStudio(api_key=KEY, client_name=name)


def test_formats_need_no_key(api: FakeAPI) -> None:
    with ConverterStudio(base_url=BASE, http_client=api.client()) as anonymous:
        formats = anonymous.formats()
        assert [f.id for f in formats] == ["heic", "mov"]
        assert anonymous.formats(".HEIC")[0].targets == ("jpg", "png")
    assert all("authorization" not in r.headers for r in api.requests)
    assert api.requests[-1].url.params["from"] == "heic"


def test_without_a_key_nothing_is_sent(api: FakeAPI, tmp_path: Path) -> None:
    with ConverterStudio(base_url=BASE, http_client=api.client()) as anonymous:
        with pytest.raises(MissingApiKeyError, match="CONVERTER_STUDIO_API_KEY"):
            anonymous.account()
        with pytest.raises(MissingApiKeyError):
            anonymous.convert(heic(tmp_path), "jpg")
    assert api.requests == []


def test_the_key_comes_from_the_environment(api: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERTER_STUDIO_API_KEY", KEY)
    monkeypatch.setenv("CONVERTER_STUDIO_BASE_URL", BASE + "/")
    with ConverterStudio(http_client=api.client()) as from_env:
        assert from_env.account().plan == "pro"
        assert from_env.base_url == BASE
        assert KEY not in repr(from_env)


def test_a_wrong_key_raises_the_api_message(api: FakeAPI) -> None:
    wrong = ConverterStudio(api_key="cvs_live_wrong", base_url=BASE, http_client=api.client())
    with wrong, pytest.raises(AuthenticationError) as caught:
        wrong.account()
    assert caught.value.code == "invalidApiKey"
    assert str(caught.value) == "This API key does not work."
    assert len(api.requests) == 1  # 401 is not retried


def test_account(client: ConverterStudio) -> None:
    account = client.account()
    assert account.credits == 1099
    assert account.wallet is not None
    assert account.wallet.balance_micros == 1_500_000


def test_convert_a_file(client: ConverterStudio, api: FakeAPI, tmp_path: Path) -> None:
    path = heic(tmp_path, 1000)
    conversion = client.convert(path, "JPG", options={"quality": "high"}, max_credits=5)
    assert conversion.is_done
    assert conversion.output_name == "IMG_1234.jpg"
    slot = json.loads(api.bodies[0])
    assert slot == {"fileName": "IMG_1234.HEIC", "size": 1000}
    put = api.of("PUT", "/api/v1/uploads/up00000001")[0]
    assert put.headers["content-length"] == "1000"
    assert put.headers["content-type"] == "application/octet-stream"
    assert "upload-offset" not in put.headers
    assert put.url.params["token"] == "tok_up00000001"
    created = api.of("POST", CONVERSIONS)[0]
    assert json.loads(created.content) == {
        "to": "jpg",
        "uploadId": "up00000001",
        "options": {"quality": "high"},
        "maxCredits": 5,
    }
    assert created.headers["idempotency-key"].startswith("cvs-py-")
    polls = api.of("GET", f"{CONVERSIONS}/up00000001")
    assert [p.url.params["wait"] for p in polls] == ["25", "25"]


def test_convert_bytes_and_file_objects(
    client: ConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    client.convert(b"%PDF-1.7 ...", "docx", file_name="scan.pdf")
    with heic(tmp_path).open("rb") as handle:
        client.convert(handle, "png")
    names = [json.loads(api.bodies[i])["fileName"] for i, r in enumerate(api.requests)
             if r.url.path == "/api/v1/uploads"]  # fmt: skip
    assert names == ["scan.pdf", "IMG_1234.HEIC"]
    with pytest.raises(ValueError, match="file_name"):
        client.convert(b"data", "png")


def test_convert_a_link(client: ConverterStudio, api: FakeAPI) -> None:
    conversion = client.convert("https://files.example/photo.heic", "jpg")
    assert conversion.is_done
    assert api.of("POST", "/api/v1/uploads") == []
    body = json.loads(api.of("POST", CONVERSIONS)[0].content)
    assert body == {"to": "jpg", "url": "https://files.example/photo.heic"}


def test_without_waiting(client: ConverterStudio, api: FakeAPI) -> None:
    created = client.convert("https://files.example/photo.heic", "jpg", wait=False)
    assert created.status == "fetching"
    assert api.of("GET", f"{CONVERSIONS}/{created.id}") == []


def test_large_files_go_in_parts(client: ConverterStudio, api: FakeAPI, tmp_path: Path) -> None:
    path = heic(tmp_path, 2500)
    upload = client.upload(path, part_size=1000)
    puts = api.of("PUT", f"/api/v1/uploads/{upload.id}")
    assert [p.headers["upload-offset"] for p in puts] == ["0", "1000", "2000"]
    assert [p.headers["content-length"] for p in puts] == ["1000", "1000", "500"]
    assert api.uploads[upload.id]["data"] == path.read_bytes()
    assert api.uploads[upload.id]["size"] == 2500


def test_a_part_is_sent_again_after_a_failure(
    client: ConverterStudio, api: FakeAPI, clock: Clock, tmp_path: Path
) -> None:
    path = heic(tmp_path, 2000)
    api.push(
        "/api/v1/uploads/up00000001",
        httpx.Response(200, json={"status": "partial"}),
        error_response(503, "serverBusy", "Busy."),
    )
    upload = client.upload(path, part_size=1000)
    puts = api.of("PUT", f"/api/v1/uploads/{upload.id}")
    assert [p.headers["upload-offset"] for p in puts] == ["0", "1000", "1000"]
    assert len(clock.sleeps) == 1
    # The first PUT was answered from the queue: the fake kept nothing of it.
    assert api.uploads[upload.id]["data"] == path.read_bytes()[1000:]


def test_a_last_part_whose_answer_was_lost_counts_as_sent(
    client: ConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    api.push(
        "/api/v1/uploads/up00000001",
        httpx.ReadTimeout("lost"),
        error_response(404, "notFound", "We couldn't find that conversion."),
    )
    upload = client.upload(heic(tmp_path))
    assert upload.id == "up00000001"


def test_network_errors_are_retried(client: ConverterStudio, api: FakeAPI, clock: Clock) -> None:
    api.push("/api/v1/account", httpx.ConnectError("refused"), httpx.ReadTimeout("slow"))
    assert client.account().plan == "pro"
    assert len(api.of("GET", "/api/v1/account")) == 3
    assert len(clock.sleeps) == 2


def test_a_retried_conversion_keeps_its_idempotency_key(
    client: ConverterStudio, api: FakeAPI, clock: Clock
) -> None:
    api.push(CONVERSIONS, error_response(502, "serverBusy", "Busy."))
    api.push(CONVERSIONS, httpx.Response(429, headers={"Retry-After": "3"}, json={}))
    client.convert("https://files.example/a.heic", "jpg", wait=False)
    keys = {r.headers["idempotency-key"] for r in api.of("POST", CONVERSIONS)}
    assert len(api.of("POST", CONVERSIONS)) == 3
    assert len(keys) == 1
    assert clock.sleeps[1] == 3.0


def test_retries_run_out(api: FakeAPI, clock: Clock) -> None:
    api.push("/api/v1/account", *[error_response(503, "serverBusy", "Busy.")] * 2)
    with ConverterStudio(api_key=KEY, base_url=BASE, http_client=api.client(), max_retries=1) as c:
        c._sleep = clock.sleep
        with pytest.raises(ServerError, match="Busy"):
            c.account()
    assert len(api.requests) == 2


def test_connection_errors_name_only_the_host(api: FakeAPI, clock: Clock) -> None:
    api.push("/api/v1/account", *[httpx.ConnectError("refused")] * 3)
    api.push("/api/v1/formats", *[httpx.ReadTimeout("slow")] * 3)
    with ConverterStudio(api_key=KEY, base_url=BASE, http_client=api.client()) as c:
        c._sleep = clock.sleep
        with pytest.raises(APIConnectionError) as refused:
            c.account()
        with pytest.raises(APITimeoutError):
            c.formats()
    assert str(refused.value) == "Could not reach api.test: refused"


def test_bad_requests_are_not_retried(client: ConverterStudio, api: FakeAPI) -> None:
    api.push(CONVERSIONS, error_response(400, "invalidUrl", "That doesn't look like a link."))
    with pytest.raises(BadRequestError) as caught:
        client.create_conversion("jpg", url="https://x")
    assert caught.value.code == "invalidUrl"
    assert len(api.of("POST", CONVERSIONS)) == 1


def test_idempotent_replay(client: ConverterStudio, api: FakeAPI) -> None:
    first = client.create_conversion("jpg", url="https://e/a.heic", idempotency_key="job-42")
    again = client.create_conversion("jpg", url="https://e/a.heic", idempotency_key="job-42")
    assert again.id == first.id
    assert again.replayed
    assert not first.replayed
    with pytest.raises(ConflictError) as caught:
        client.create_conversion("png", url="https://e/a.heic", idempotency_key="job-42")
    assert caught.value.code == "idempotencyKeyReused"
    with pytest.raises(ValueError, match="idempotency_key"):
        client.create_conversion("png", url="https://e/a.heic", idempotency_key="not valid")


def test_a_failed_conversion(client: ConverterStudio, api: FakeAPI, tmp_path: Path) -> None:
    api.statuses = ["processing", "failed"]
    api.error = {"code": "conversionFailed", "message": "Something went wrong while converting."}
    with pytest.raises(ConversionFailedError) as caught:
        client.convert(heic(tmp_path), "jpg")
    assert caught.value.code == "conversionFailed"
    assert str(caught.value) == "Something went wrong while converting."
    assert caught.value.conversion.status == "failed"


@pytest.mark.parametrize(("status", "code"), [("expired", "fileExpired"), ("canceled", "canceled")])
def test_expired_and_canceled(
    client: ConverterStudio, api: FakeAPI, status: str, code: str
) -> None:
    api.statuses = [status]
    with pytest.raises(ConversionFailedError) as caught:
        client.convert("https://e/a.heic", "jpg")
    assert caught.value.code == code


def test_a_price_over_the_limit_waits_for_a_go_ahead(client: ConverterStudio, api: FakeAPI) -> None:
    api.credits = 34
    with pytest.raises(ConfirmationRequiredError) as caught:
        client.convert("https://e/long.mov", "mp4")
    quoted = caught.value
    assert quoted.credits == 34
    assert quoted.max_credits == 20
    assert "34 credits" in str(quoted)
    done = client.wait(client.confirm(quoted.conversion))
    assert done.is_done
    assert api.of("POST", f"{CONVERSIONS}/{done.id}/confirm")


def test_waiting_times_out(client: ConverterStudio, api: FakeAPI, clock: Clock) -> None:
    api.statuses = ["processing"]
    created = client.convert("https://e/a.mov", "mp4", wait=False)
    with pytest.raises(ConversionTimeoutError) as caught:
        client.wait(created, timeout=12)
    assert caught.value.conversion.id == created.id
    assert "12 seconds" in str(caught.value)
    waits = [int(r.url.params["wait"]) for r in api.of("GET", f"{CONVERSIONS}/{created.id}")]
    assert waits[0] == 12
    assert all(w >= 1 for w in waits)
    # The fake answers at once: the pauses grow, and stay within the time given.
    assert clock.sleeps[:3] == [1.0, 2.0, 4.0]
    assert sum(clock.sleeps) <= 12


def test_long_polls_that_take_their_time_need_no_pause(api: FakeAPI, clock: Clock) -> None:
    api.statuses = ["processing", "processing", "done"]

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("wait"):
            clock.now += float(request.url.params["wait"])
        return api.handle(request)

    with ConverterStudio(
        api_key=KEY, base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(slow))
    ) as c:
        c._sleep, c._clock = clock.sleep, clock
        assert c.convert("https://e/a.mov", "mp4", timeout=600).is_done
    assert clock.sleeps == []


def test_get_conversion(client: ConverterStudio, api: FakeAPI) -> None:
    created = client.create_conversion("jpg", url="https://e/a.heic")
    client.get_conversion(created, wait=600)
    client.get_conversion(created.id)
    polls = api.of("GET", f"{CONVERSIONS}/{created.id}")
    assert polls[0].url.params["wait"] == "60"
    assert "wait" not in polls[1].url.params
    with pytest.raises(ValueError, match="id"):
        client.get_conversion("")


def test_download_into_a_folder(client: ConverterStudio, tmp_path: Path) -> None:
    conversion = client.convert(heic(tmp_path), "jpg")
    out = tmp_path / "out"
    out.mkdir()
    first = client.download(conversion, out)
    second = client.download(conversion, out)
    third = client.download(conversion, out, overwrite=True)
    assert first == out / "IMG_1234.jpg"
    assert second == out / "IMG_1234-1.jpg"
    assert third == first
    assert first.read_bytes() == b"RESULT"
    assert sorted(p.name for p in out.iterdir()) == ["IMG_1234-1.jpg", "IMG_1234.jpg"]


def test_download_to_a_path(
    client: ConverterStudio, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversion = client.convert(heic(tmp_path), "jpg")
    target = tmp_path / "deep" / "photo.jpeg"
    assert client.download(conversion, target) == target
    with pytest.raises(FileExistsError):
        client.download(conversion, target)
    assert client.download(conversion, target, overwrite=True) == target
    monkeypatch.chdir(tmp_path)
    assert client.download(conversion) == tmp_path / "IMG_1234.jpg"


def test_a_result_name_cannot_leave_the_folder(
    client: ConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    api.output_name = "../../evil.sh"
    out = tmp_path / "out"
    out.mkdir()
    path = client.download(client.convert(heic(tmp_path), "jpg"), out)
    assert path == out / "evil.sh"


def test_a_download_that_fails_leaves_nothing(
    client: ConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    conversion = client.convert(heic(tmp_path), "jpg")
    out = tmp_path / "out"
    out.mkdir()
    api.push(
        f"/api/jobs/{conversion.id}/download", httpx.Response(410, json={"error": "fileExpired"})
    )
    with pytest.raises(GoneError) as caught:
        client.download(conversion, out)
    assert caught.value.code == "fileExpired"
    assert list(out.iterdir()) == []


def test_a_broken_download_is_started_again(
    client: ConverterStudio, api: FakeAPI, tmp_path: Path
) -> None:
    conversion = client.convert(heic(tmp_path), "jpg")
    api.push(f"/api/jobs/{conversion.id}/download", httpx.RemoteProtocolError("cut"))
    path = client.download(conversion, tmp_path)
    assert path.read_bytes() == b"RESULT"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".part")] == []


def test_results_by_id_and_in_memory(client: ConverterStudio, api: FakeAPI) -> None:
    api.output = b"x" * 3_000_000
    conversion = client.convert("https://e/a.heic", "jpg")
    chunks = list(client.iter_download(conversion.id, chunk_size=1_000_000))
    assert [len(c) for c in chunks] == [1_000_000] * 3
    assert client.download_bytes(conversion) == api.output


def test_only_done_conversions_download(client: ConverterStudio, api: FakeAPI) -> None:
    api.statuses = ["processing"]
    created = client.convert("https://e/a.heic", "jpg", wait=False)
    with pytest.raises(ConverterStudioError, match="not done yet"):
        client.download_bytes(created)
    failed = Conversion.from_api({"id": "x", "status": "failed", "error": None})
    with pytest.raises(ConversionFailedError):
        client.download(failed)


def test_close_leaves_a_given_http_client_open(api: FakeAPI) -> None:
    http = api.client()
    with ConverterStudio(api_key=KEY, base_url=BASE, http_client=http):
        pass
    assert not http.is_closed
    own = ConverterStudio(api_key=KEY, base_url=BASE)
    own.close()
    assert own._http.is_closed


def test_settings_are_checked(api: FakeAPI) -> None:
    with pytest.raises(ValueError, match="max_retries"):
        ConverterStudio(max_retries=-1)
    with ConverterStudio(api_key=KEY, base_url=BASE, timeout=5, http_client=api.client()) as c:
        assert c._timeout.read == 5
