from __future__ import annotations

from collections.abc import Iterator

import pytest

from converter_studio import ConverterStudio

from .fake_api import BASE, KEY, FakeAPI


class Clock:
    """A clock that only moves when the client sleeps or a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVERTER_STUDIO_API_KEY", raising=False)
    monkeypatch.delenv("CONVERTER_STUDIO_BASE_URL", raising=False)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(api: FakeAPI, clock: Clock) -> Iterator[ConverterStudio]:
    with ConverterStudio(api_key=KEY, base_url=BASE, http_client=api.client()) as c:
        c._sleep = clock.sleep
        c._clock = clock
        yield c


def _backends() -> list[str]:
    try:
        import trio  # noqa: F401  (needs ctypes, which some Python builds lack)
    except ImportError:
        return ["asyncio"]
    return ["asyncio", "trio"]


@pytest.fixture(params=_backends())
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)
