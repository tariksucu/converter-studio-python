"""What the Converter Studio API returns, as typed, read-only objects.

Every object keeps the JSON it was made from in ``raw``, so fields the API adds later are
reachable before this package names them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeAlias

ConversionStatus: TypeAlias = Literal[
    "fetching",
    "waiting_for_upload",
    "measuring",
    "quoted",
    "queued",
    "processing",
    "done",
    "failed",
    "expired",
    "canceled",
]
"""Where a conversion stands. ``quoted``: it costs more than ``max_credits`` and waits for
:meth:`~converter_studio.ConverterStudio.confirm`."""

#: Statuses a conversion never leaves.
FINISHED_STATUSES: frozenset[str] = frozenset({"done", "failed", "expired", "canceled"})
#: Statuses that need nobody to wait any longer: finished, or waiting for a go-ahead.
SETTLED_STATUSES: frozenset[str] = FINISHED_STATUSES | {"quoted"}

ParamValue: TypeAlias = str | int | float


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _bool(value: object) -> bool:
    return value is True


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _params(value: object) -> dict[str, ParamValue]:
    params: dict[str, ParamValue] = {}
    for key, item in _object(value).items():
        if isinstance(item, str | int | float) and not isinstance(item, bool):
            params[str(key)] = item
    return params


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Why a conversion failed, as the API says it: a stable ``code`` and an English
    ``message`` meant to be shown to people."""

    code: str
    message: str
    params: Mapping[str, ParamValue] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: object) -> ErrorInfo | None:
        body = _object(data)
        code = _str(body.get("code"))
        if code is None:
            return None
        return cls(
            code=code, message=_str(body.get("message")) or code, params=_params(body.get("params"))
        )


@dataclass(frozen=True, slots=True)
class Conversion:
    """A conversion (``GET /v1/conversions/{id}``). Its id is also its upload's id."""

    id: str
    status: ConversionStatus
    from_format: str | None
    to_format: str | None
    input_name: str | None
    input_size: int | None
    #: The price in credits, once known.
    credits: int | None
    #: It goes ahead on its own up to this price; ``None``: it waits for a go-ahead.
    max_credits: int | None
    #: From 0 to 1 while it converts.
    progress: float | None
    output_name: str | None
    output_size: int | None
    #: Anyone with this link can download the result until ``expires_at``.
    download_url: str | None = field(repr=False)
    expires_at: datetime | None
    error: ErrorInfo | None
    #: What to do next, in a sentence.
    next: str
    #: The API answered a repeated request (same ``Idempotency-Key``) with this conversion.
    replayed: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: object, *, replayed: bool = False) -> Conversion:
        body = _object(data)
        return cls(
            id=_str(body.get("id")) or "",
            status=_str(body.get("status")) or "failed",  # type: ignore[arg-type]
            from_format=_str(body.get("from")),
            to_format=_str(body.get("to")),
            input_name=_str(body.get("inputName")),
            input_size=_int(body.get("inputSize")),
            credits=_int(body.get("credits")),
            max_credits=_int(body.get("maxCredits")),
            progress=_float(body.get("progress")),
            output_name=_str(body.get("outputName")),
            output_size=_int(body.get("outputSize")),
            download_url=_str(body.get("downloadUrl")),
            expires_at=_time(body.get("expiresAt")),
            error=ErrorInfo.from_api(body.get("error")),
            next=_str(body.get("next")) or "",
            replayed=replayed,
            raw=dict(body),
        )

    @property
    def is_done(self) -> bool:
        """The result is ready to download."""
        return self.status == "done"

    @property
    def is_finished(self) -> bool:
        """It will not change any more: done, failed, expired or canceled."""
        return self.status in FINISHED_STATUSES

    @property
    def needs_confirmation(self) -> bool:
        """It costs more than ``max_credits`` and waits for :meth:`confirm`."""
        return self.status == "quoted"


@dataclass(frozen=True, slots=True)
class Upload:
    """An upload slot (``POST /v1/uploads``); its file is sent to ``upload_url``."""

    id: str
    upload_url: str = field(repr=False)
    method: str
    max_bytes: int | None
    #: One request carries at most this many bytes; larger files go in parts.
    part_bytes: int | None
    expires_at: datetime | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: object) -> Upload:
        body = _object(data)
        return cls(
            id=_str(body.get("id")) or "",
            upload_url=_str(body.get("uploadUrl")) or "",
            method=_str(body.get("method")) or "PUT",
            max_bytes=_int(body.get("maxBytes")),
            part_bytes=_int(body.get("partBytes")),
            expires_at=_time(body.get("expiresAt")),
            raw={key: value for key, value in body.items() if key not in ("uploadUrl", "curl")},
        )


@dataclass(frozen=True, slots=True)
class Format:
    """A format the API reads, and what it converts it to right now."""

    id: str
    name: str
    category: str
    extensions: tuple[str, ...]
    #: The formats it converts to, the most popular first.
    targets: tuple[str, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: object) -> Format:
        body = _object(data)
        format_id = _str(body.get("id")) or ""
        return cls(
            id=format_id,
            name=_str(body.get("name")) or format_id,
            category=_str(body.get("category")) or "",
            extensions=_strings(body.get("extensions")),
            targets=_strings(body.get("to")),
            raw=dict(body),
        )


@dataclass(frozen=True, slots=True)
class Allowance:
    """The plan's credits this period."""

    used: int
    limit: int
    #: ``day`` or ``month``.
    per: str


@dataclass(frozen=True, slots=True)
class Wallet:
    """The web wallet, which pays for cloud work past the credits when allowed."""

    #: The balance in millionths of a US dollar.
    balance_micros: int
    #: Whether it pays once the credits run out.
    pays: bool


@dataclass(frozen=True, slots=True)
class Account:
    """The account of the API key (``GET /v1/account``)."""

    email: str
    plan: str
    #: Converter Studio on every platform (the app plan, Pro or Premium).
    all_apps: bool
    #: Credits left: the plan's plus packs.
    credits: int
    allowance: Allowance | None
    #: Credits left in packs (they never expire).
    packs: int
    renews_at: datetime | None
    max_file_bytes: int | None
    max_media_seconds: int | None
    wallet: Wallet | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: object) -> Account:
        body = _object(data)
        allowance = _object(body.get("allowance"))
        wallet = body.get("wallet")
        return cls(
            email=_str(body.get("email")) or "",
            plan=_str(body.get("plan")) or "",
            all_apps=_bool(body.get("allApps")),
            credits=_int(body.get("credits")) or 0,
            allowance=Allowance(
                used=_int(allowance.get("used")) or 0,
                limit=_int(allowance.get("limit")) or 0,
                per=_str(allowance.get("per")) or "",
            )
            if allowance
            else None,
            packs=_int(body.get("packs")) or 0,
            renews_at=_time(body.get("renewsAt")),
            max_file_bytes=_int(body.get("maxFileBytes")),
            max_media_seconds=_int(body.get("maxMediaSeconds")),
            wallet=Wallet(
                balance_micros=_int(_object(wallet).get("balanceMicros")) or 0,
                pays=_bool(_object(wallet).get("pays")),
            )
            if isinstance(wallet, Mapping)
            else None,
            raw=dict(body),
        )
