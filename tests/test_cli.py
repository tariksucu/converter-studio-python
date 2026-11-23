from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from converter_studio import ConverterStudio, __version__, _cli

from .conftest import Clock
from .fake_api import BASE, KEY, FakeAPI, error_response

CONVERSIONS = "/api/v1/conversions"
Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def run(
    api: FakeAPI, clock: Clock, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Run:
    """Runs the command with the fake API; returns (exit code, stdout, stderr)."""

    def make_client(args: argparse.Namespace) -> ConverterStudio:
        if args.api_key_file is not None:
            key = args.api_key_file.read_text().strip() or None
        else:
            key = None
        client = ConverterStudio(
            api_key=key, base_url=args.base_url or BASE, http_client=api.client()
        )
        client._sleep, client._clock = clock.sleep, clock
        return client

    original = _cli.make_client

    def checked(args: argparse.Namespace) -> ConverterStudio:
        original(args).close()  # the real checks (an empty key file), then the fake API
        return make_client(args)

    monkeypatch.setattr(_cli, "make_client", checked)
    monkeypatch.setenv("CONVERTER_STUDIO_API_KEY", KEY)

    def invoke(*argv: str) -> tuple[int, str, str]:
        code = _cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return invoke


def files(tmp_path: Path, *names: str) -> list[str]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"data-" + name.encode())
        paths.append(str(path))
    return paths


def test_convert_several_files(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    inputs = files(tmp_path, "a.heic", "b.heic")
    out = tmp_path / "out"
    code, stdout, stderr = run("convert", *inputs, "--to", "JPG", "--output-dir", str(out))
    assert code == 0
    assert stdout.splitlines() == [str(out / "a.jpg"), str(out / "b.jpg")]
    assert "[1/2] a.heic -> jpg" in stderr
    assert "saved" in stderr
    assert (out / "a.jpg").read_bytes() == b"RESULT"
    assert KEY not in stdout + stderr


def test_options_are_typed(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    (source,) = files(tmp_path, "clip.mov")
    code, _, _ = run(
        "convert", source, "-t", "mp4", "-o", str(tmp_path),
        "-O", "height=720", "-O", "maxSize=25", "-O", "mute=true", "-O", "custom:=[1, 2]",
        "--max-credits", "40",
    )  # fmt: skip
    assert code == 0
    body = json.loads(api.of("POST", CONVERSIONS)[0].content)
    assert body["options"] == {"height": "720", "maxSize": 25, "mute": True, "custom": [1, 2]}
    assert body["maxCredits"] == 40


@pytest.mark.parametrize(
    "argv",
    [
        ["-O", "quality"],
        ["-O", "mute=perhaps"],
        ["-O", "x:={bad"],
        ["--max-credits", "-1"],
    ],
)
def test_wrong_usage(run: Run, api: FakeAPI, tmp_path: Path, argv: list[str]) -> None:
    (source,) = files(tmp_path, "a.heic")
    code, _, stderr = run("convert", source, "--to", "jpg", *argv)
    assert code == 2
    assert "converter-studio:" in stderr
    assert api.requests == []


def test_a_missing_file_stops_before_anything_is_sent(
    run: Run, api: FakeAPI, tmp_path: Path
) -> None:
    (source,) = files(tmp_path, "a.heic")
    code, _, stderr = run("convert", source, str(tmp_path / "nope.heic"), "--to", "jpg")
    assert code == 2
    assert "no such file" in stderr
    assert api.requests == []


def test_a_failure_does_not_stop_the_other_files(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    first, second = files(tmp_path, "a.heic", "b.heic")
    api.push(
        CONVERSIONS,
        error_response(422, "unsupportedConversion", "We can't convert heic to mp3 yet."),
    )
    code, stdout, stderr = run("convert", first, second, "--to", "mp3", "-o", str(tmp_path))
    assert code == 1
    assert "a.heic: We can't convert heic to mp3 yet. [unsupportedConversion]" in stderr
    assert stdout.splitlines() == [str(tmp_path / "b.mp3")]


def test_a_failed_conversion(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    api.statuses = ["processing", "failed"]
    api.error = {"code": "conversionFailed", "message": "Something went wrong."}
    (source,) = files(tmp_path, "a.heic")
    code, stdout, stderr = run("convert", source, "--to", "jpg", "-o", str(tmp_path))
    assert code == 1
    assert "Something went wrong. [conversionFailed]" in stderr
    assert stdout == ""


def test_a_wrong_key(
    run: Run, api: FakeAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "cvs_live_this_must_never_be_printed"
    key_file = tmp_path / "key"
    key_file.write_text(secret + "\n")
    inputs = files(tmp_path, "a.heic", "b.heic")
    code, stdout, stderr = run("convert", *inputs, "--to", "jpg", "--api-key-file", str(key_file))
    assert code == 3
    assert "This API key does not work. [invalidApiKey]" in stderr
    assert secret not in stdout + stderr
    assert len(api.of("POST", "/api/v1/uploads")) == 1  # the second file is not tried


def test_no_key(run: Run, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVERTER_STUDIO_API_KEY")
    code, _, stderr = run("account")
    assert code == 3
    assert "CONVERTER_STUDIO_API_KEY" in stderr
    empty = tmp_path / "empty"
    empty.write_text("\n")
    code, _, stderr = run("account", "--api-key-file", str(empty))
    assert code == 3
    assert "is empty" in stderr


def test_credits(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    (source,) = files(tmp_path, "a.mov")
    api.credits = 34
    code, _, stderr = run("convert", source, "--to", "mp4", "-o", str(tmp_path))
    assert code == 4
    assert "--max-credits 34" in stderr
    api.push(
        CONVERSIONS,
        error_response(402, "notEnoughCredits", "This needs 34 credits and you have 2."),
    )
    code, _, stderr = run("convert", source, "--to", "mp4", "--max-credits", "40")
    assert code == 4
    assert "[notEnoughCredits]" in stderr


def test_timeout(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    (source,) = files(tmp_path, "a.mov")
    api.statuses = ["processing"]
    code, _, stderr = run("convert", source, "--to", "mp4", "--timeout", "30")
    assert code == 5
    assert "did not finish within 30 seconds" in stderr


def test_json_output(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    first, second = files(tmp_path, "a.heic", "b.heic")
    api.push(CONVERSIONS, error_response(422, "unsupportedConversion", "No."))
    code, stdout, stderr = run(
        "convert", first, second, "--to", "jpg", "--json", "-o", str(tmp_path)
    )
    assert code == 1
    lines = [json.loads(line) for line in stdout.splitlines()]
    assert lines[0]["error"] == {"code": "unsupportedConversion", "message": "No.", "status": 422}
    assert lines[1]["output"] == str(tmp_path / "b.jpg")
    assert lines[1]["conversion"]["status"] == "done"
    assert "downloadUrl" not in lines[1]["conversion"]
    assert "tok_" not in stdout
    assert "[1/2]" not in stderr


def test_quiet(run: Run, tmp_path: Path) -> None:
    (source,) = files(tmp_path, "a.heic")
    code, stdout, stderr = run("convert", source, "--to", "jpg", "-q", "-o", str(tmp_path))
    assert code == 0
    assert stderr == ""
    assert stdout.strip() == str(tmp_path / "a.jpg")


def test_formats(run: Run, api: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONVERTER_STUDIO_API_KEY")
    code, stdout, _ = run("formats")
    assert code == 0
    assert stdout.splitlines()[0].split() == ["heic", "HEIC", "image", "->", "jpg,", "png"]
    code, stdout, _ = run("formats", "--from", "mov", "--json")
    assert json.loads(stdout)[0]["to"] == ["mp4"]
    code, _, stderr = run("formats", "--from", "xyz")
    assert code == 1
    assert "no such format: xyz" in stderr
    api.push("/api/v1/formats", error_response(400, "invalidRequest", "Not right."))
    code, _, stderr = run("formats", "--from", "abc")
    assert code == 1
    assert "no such format: abc" in stderr


def test_a_request_the_api_refuses(run: Run, api: FakeAPI, tmp_path: Path) -> None:
    (source,) = files(tmp_path, "a.heic")
    api.push(CONVERSIONS, error_response(400, "invalidRequest", "Not right."))
    code, _, stderr = run("convert", source, "--to", "jpgg")
    assert code == 1
    assert "Not right. [invalidRequest] Check the target format" in stderr


def test_account(run: Run) -> None:
    code, stdout, _ = run("account")
    assert code == 0
    assert "dev@example.com" in stdout
    assert "1099 left (1 of 1100 used this month)" in stdout
    assert "$1.50, pays past the credits" in stdout
    code, stdout, _ = run("account", "--json")
    assert json.loads(stdout)["plan"] == "pro"


def test_an_output_folder_that_is_a_file(run: Run, tmp_path: Path) -> None:
    (source,) = files(tmp_path, "a.heic")
    blocker = tmp_path / "out"
    blocker.write_text("")
    code, _, stderr = run("convert", source, "--to", "jpg", "-o", str(blocker))
    assert code == 1
    assert "converter-studio:" in stderr


def test_a_wrong_base_url(run: Run) -> None:
    code, _, stderr = run("account", "--base-url", "ftp://example.com")
    assert code == 2
    assert "base_url" in stderr


def test_version_and_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "converter_studio", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == f"converter-studio {__version__}"
    result = subprocess.run(
        [sys.executable, "-m", "converter_studio", "convert", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--output-dir" in result.stdout
    missing = subprocess.run(
        [sys.executable, "-m", "converter_studio", "convert", "a.heic"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
