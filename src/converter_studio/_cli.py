"""``converter-studio``: convert files from the command line.

Exit codes: 0 all done; 1 a conversion or request failed; 2 wrong usage; 3 no API key, or the key
does not work; 4 not enough credits, or the price is over ``--max-credits``; 5 a conversion did
not finish within ``--timeout``; 130 interrupted. With several files, the first failure's code.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from . import _base
from ._client import ConverterStudio
from ._errors import (
    APIError,
    AuthenticationError,
    BadRequestError,
    ConfirmationRequiredError,
    ConversionFailedError,
    ConversionTimeoutError,
    ConverterStudioError,
    InsufficientCreditsError,
    MissingApiKeyError,
)
from ._models import Account, Conversion, Format
from ._version import __version__

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_KEY = 3
EXIT_CREDITS = 4
EXIT_TIMEOUT = 5
EXIT_INTERRUPTED = 130

DOCS = "https://converter.studio/developers"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base-url",
        metavar="URL",
        help=f"the API's address (default: ${_base.BASE_URL_ENV} or {_base.DEFAULT_BASE_URL})",
    )
    common.add_argument(
        "--api-key-file",
        type=Path,
        metavar="PATH",
        help=f"read the API key from this file instead of ${_base.API_KEY_ENV}",
    )
    common.add_argument("--json", action="store_true", help="print JSON instead of text")

    parser = argparse.ArgumentParser(
        prog="converter-studio",
        description="Convert files with Converter Studio (https://converter.studio).",
        epilog=(
            f"The API key is read from ${_base.API_KEY_ENV}; create one at "
            f"https://converter.studio/account#api-keys. Documentation: {DOCS}"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    convert = commands.add_parser(
        "convert",
        parents=[common],
        help="convert files or links to another format",
        description=(
            "Convert local files or links (http:// or https://) to another format and save the "
            "results. Results keep their names with the new extension; an existing file is "
            "never replaced unless --overwrite (the result gets -1, -2... instead)."
        ),
        epilog=(
            "Options are the API's conversion settings, for example -O quality=high, "
            f"-O maxSize=25, -O keepMetadata=true (see {DOCS})."
        ),
    )
    convert.add_argument("inputs", nargs="+", metavar="FILE_OR_URL", help="files or links")
    convert.add_argument("-t", "--to", required=True, metavar="FORMAT", help="target format")
    convert.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path(),
        metavar="DIR",
        help="where to save the results (default: the current folder)",
    )
    convert.add_argument(
        "-O",
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a conversion setting (repeat for more); KEY:=JSON sends a JSON value",
    )
    convert.add_argument(
        "--max-credits",
        type=int,
        metavar="N",
        help="go ahead on its own up to this price (the API's default: 20; 0 only prices it)",
    )
    convert.add_argument(
        "--timeout",
        type=float,
        default=_base.DEFAULT_WAIT_TIMEOUT,
        metavar="SECONDS",
        help="how long to wait for each conversion (default: %(default)g)",
    )
    convert.add_argument("--overwrite", action="store_true", help="replace existing files")
    convert.add_argument("-q", "--quiet", action="store_true", help="print only errors")

    formats = commands.add_parser(
        "formats",
        parents=[common],
        help="list the formats and what they convert to (no key needed)",
    )
    formats.add_argument("--from", dest="from_format", metavar="FORMAT", help="only this format")

    commands.add_parser("account", parents=[common], help="show the key's plan and credits")
    return parser


def make_client(args: argparse.Namespace) -> ConverterStudio:
    """The client for a command (tests replace it)."""
    api_key = None
    if args.api_key_file is not None:
        api_key = args.api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise MissingApiKeyError(f"The API key file {args.api_key_file} is empty.")
    return ConverterStudio(api_key=api_key, base_url=args.base_url)


def parse_options(pairs: Sequence[str]) -> dict[str, object]:
    options: dict[str, object] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--option takes KEY=VALUE, not {pair!r}")
        key = key.strip()
        if key.endswith(":"):
            key = key[:-1].strip()
            try:
                options[key] = json.loads(value)
            except ValueError:
                raise ValueError(f"{key}:= takes a JSON value, not {value!r}") from None
        else:
            options[key] = _base.parse_option(key, value)
    return options


def exit_code(error: BaseException) -> int:
    if isinstance(error, MissingApiKeyError | AuthenticationError):
        return EXIT_KEY
    if isinstance(error, InsufficientCreditsError | ConfirmationRequiredError):
        return EXIT_CREDITS
    if isinstance(error, ConversionTimeoutError):
        return EXIT_TIMEOUT
    return EXIT_FAILED


def describe(error: BaseException) -> str:
    """An error in one line: the API's message, and its code when there is one."""
    code = getattr(error, "code", None)
    if isinstance(error, ConfirmationRequiredError):
        return f"{error.message} Run again with --max-credits {error.credits} to go ahead."
    if isinstance(error, BadRequestError) and code == "invalidRequest":
        return (
            f"{error.message} [{code}] Check the target format (converter-studio formats) and "
            "the options."
        )
    if isinstance(error, ConverterStudioError):
        return f"{error.message} [{code}]" if isinstance(code, str) else error.message
    return str(error) or type(error).__name__


def human_size(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"  # pragma: no cover


class Output:
    def __init__(self, *, as_json: bool, quiet: bool, out: TextIO, err: TextIO) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self.out = out
        self.err = err

    def note(self, text: str) -> None:
        if not self.quiet and not self.as_json:
            print(text, file=self.err, flush=True)

    def error(self, text: str) -> None:
        print(f"converter-studio: {text}", file=self.err, flush=True)

    def json(self, value: object) -> None:
        print(json.dumps(value, ensure_ascii=False, default=str), file=self.out, flush=True)


def run_convert(args: argparse.Namespace, client: ConverterStudio, output: Output) -> int:
    try:
        options = parse_options(args.option)
    except ValueError as exc:
        output.error(str(exc))
        return EXIT_USAGE
    missing = [item for item in args.inputs if not _base.is_url(item) and not Path(item).is_file()]
    if missing:
        output.error("no such file: " + ", ".join(missing))
        return EXIT_USAGE
    if args.max_credits is not None and args.max_credits < 0:
        output.error("--max-credits must be 0 or more")
        return EXIT_USAGE
    args.output_dir.mkdir(parents=True, exist_ok=True)
    target = args.to.lower().lstrip(".")
    first_failure = EXIT_OK
    total = len(args.inputs)
    for index, item in enumerate(args.inputs, start=1):
        source: str | Path = item if _base.is_url(item) else Path(item)
        label = item if _base.is_url(item) else Path(item).name
        output.note(f"[{index}/{total}] {label} -> {target}")
        conversion: Conversion | None = None
        try:
            conversion = client.convert(
                source,
                target,
                options=options or None,
                max_credits=args.max_credits,
                timeout=args.timeout,
            )
            path = client.download(conversion, args.output_dir, overwrite=args.overwrite)
        except (ConverterStudioError, OSError) as exc:
            code = exit_code(exc)
            first_failure = first_failure or code
            if isinstance(exc, ConversionFailedError | ConfirmationRequiredError):
                conversion = exc.conversion
            output.error(f"{label}: {describe(exc)}")
            if args.json:
                output.json(
                    {"input": item, "error": _error_json(exc), **_conversion_json(conversion)}
                )
            # A key that does not work, or no credits: the next files would fail the same way.
            if code == EXIT_KEY or isinstance(exc, InsufficientCreditsError):
                break
            continue
        credits = conversion.credits
        output.note(
            f"  saved {path} ({human_size(conversion.output_size)}, "
            f"{credits} credit{'' if credits == 1 else 's'})"
        )
        if args.json:
            output.json({"input": item, "output": str(path), **_conversion_json(conversion)})
        else:
            print(path, file=output.out, flush=True)
    return first_failure


def _conversion_json(conversion: Conversion | None) -> dict[str, Any]:
    if conversion is None:
        return {}
    raw = {key: value for key, value in conversion.raw.items() if key != "downloadUrl"}
    return {"conversion": raw}


def _error_json(error: BaseException) -> dict[str, Any]:
    code = getattr(error, "code", None)
    message = error.message if isinstance(error, ConverterStudioError) else str(error)
    result: dict[str, Any] = {"code": code if isinstance(code, str) else None, "message": message}
    if isinstance(error, APIError):
        result["status"] = error.status_code
    return result


def run_formats(args: argparse.Namespace, client: ConverterStudio, output: Output) -> int:
    try:
        formats = client.formats(args.from_format)
    except BadRequestError:
        if not args.from_format:
            raise
        formats = []
    if args.json:
        output.json([dict(item.raw) for item in formats])
        return EXIT_OK
    if not formats:
        output.error(f"no such format: {args.from_format}" if args.from_format else "no formats")
        return EXIT_FAILED
    width = max(len(item.id) for item in formats)
    name_width = max(len(item.name) for item in formats)
    for item in formats:
        print(_format_line(item, width, name_width), file=output.out)
    return EXIT_OK


def _format_line(item: Format, width: int, name_width: int) -> str:
    targets = ", ".join(item.targets) or "-"
    return f"{item.id:<{width}}  {item.name:<{name_width}}  {item.category:<12}  -> {targets}"


def run_account(args: argparse.Namespace, client: ConverterStudio, output: Output) -> int:
    account = client.account()
    if args.json:
        output.json(dict(account.raw))
        return EXIT_OK
    for label, value in _account_lines(account):
        print(f"{label + ':':<10} {value}", file=output.out)
    return EXIT_OK


def _account_lines(account: Account) -> list[tuple[str, str]]:
    lines = [
        ("Account", account.email),
        ("Plan", f"{account.plan}" + (" (All Apps)" if account.all_apps else "")),
    ]
    credits = f"{account.credits} left"
    if account.allowance is not None:
        credits += (
            f" ({account.allowance.used} of {account.allowance.limit} used this "
            f"{account.allowance.per or 'period'}"
            + (f", {account.packs} in packs" if account.packs else "")
            + ")"
        )
    if account.renews_at is not None:
        credits += f", renews {account.renews_at.date().isoformat()}"
    lines.append(("Credits", credits))
    if account.max_file_bytes is not None:
        limit = f"files up to {human_size(account.max_file_bytes)}"
        if account.max_media_seconds:
            limit += f", audio and video up to {account.max_media_seconds // 60} min"
        lines.append(("Limits", limit))
    if account.wallet is not None:
        lines.append(
            (
                "Wallet",
                f"${account.wallet.balance_micros / 1_000_000:.2f}"
                + (", pays past the credits" if account.wallet.pays else ""),
            )
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = Output(
        as_json=args.json, quiet=getattr(args, "quiet", False), out=sys.stdout, err=sys.stderr
    )
    try:
        client = make_client(args)
    except (ValueError, OSError, ConverterStudioError) as exc:
        output.error(describe(exc))
        return EXIT_KEY if isinstance(exc, MissingApiKeyError | OSError) else EXIT_USAGE
    try:
        with client:
            if args.command == "convert":
                return run_convert(args, client, output)
            if args.command == "formats":
                return run_formats(args, client, output)
            return run_account(args, client, output)
    except KeyboardInterrupt:
        output.error("interrupted")
        return EXIT_INTERRUPTED
    except ConverterStudioError as exc:
        output.error(describe(exc))
        return exit_code(exc)
    except OSError as exc:
        output.error(str(exc))
        return EXIT_FAILED
