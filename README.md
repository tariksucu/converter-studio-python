# Converter Studio for Python

The official Python client and command-line tool for the
[Converter Studio](https://converter.studio) API. Convert iPhone photos (HEIC), camera RAW photos,
videos, audio, documents, spreadsheets, presentations, e-books, subtitles, fonts, data files and
archives (67 formats) from your Python code or your terminal. The conversion runs on Converter
Studio's servers, so there is nothing else to install: no ImageMagick, no FFmpeg, no LibreOffice.

```python
from converter_studio import ConverterStudio

with ConverterStudio() as client:  # the API key comes from CONVERTER_STUDIO_API_KEY
    conversion = client.convert("IMG_1234.HEIC", "jpg")
    client.download(conversion, "photos/")  # photos/IMG_1234.jpg
```

```console
$ converter-studio convert IMG_1234.HEIC IMG_1235.HEIC --to jpg --output-dir photos
```

- Typed (`py.typed`, checked with mypy in strict mode), with a synchronous and an asynchronous
  client (asyncio or Trio).
- Local files, bytes, file objects or links (direct links and Google Drive, Dropbox or OneDrive
  share links), with any of the API's conversion settings.
- Large files are uploaded in parts on their own; results are streamed to disk.
- Waiting with long polling and a back-off, retries with an `Idempotency-Key` so a retry never
  starts or charges a second conversion, and errors that carry the API's code and message.
- Two dependencies: [HTTPX](https://www.python-httpx.org) and AnyIO (which HTTPX uses anyway).

## Installation

```console
$ pip install converter-studio
```

Python 3.11 or later. Then create an API key on your
[account page](https://converter.studio/account#api-keys) (keys start with `cvs_live_`; keep
them secret) and put it in the environment:

```console
$ export CONVERTER_STUDIO_API_KEY=cvs_live_...
```

Every conversion uses your account's credits, the same as on the website: free accounts get
credits every day, Pro and Premium their monthly credits. Most files cost 1 credit; audio and video
are priced by their length (video also by its frame size and the format made) once Converter
Studio has looked at them. See [pricing](https://converter.studio/pricing).

## Python

### Convert a file

```python
from converter_studio import ConverterStudio

client = ConverterStudio()  # or ConverterStudio(api_key="cvs_live_...")

conversion = client.convert("talk.mov", "mp4", options={"height": "720"})
print(conversion.status, conversion.credits, conversion.output_name)  # done 1 talk.mp4
path = client.download(conversion, "out/")  # a folder (ending in / or existing), or a file path
```

`convert()` uploads the file, starts the conversion and waits until it is done (10 minutes at
most by default: `timeout=`). The source can be:

- a path (`str` or `pathlib.Path`);
- bytes, with a name: `client.convert(data, "pdf", file_name="report.docx")`;
- a binary file object: `client.convert(open("song.flac", "rb"), "mp3")`;
- a link (a `str` starting with `http://` or `https://`): Converter Studio fetches the file
  itself, so it never passes through your machine.

`download()` never replaces an existing file in a folder unless `overwrite=True`: the result gets
`-1`, `-2`... before its extension. The file appears only once it is complete. You can also stream
the result (`for chunk in client.iter_download(conversion): ...`) or get it in memory
(`client.download_bytes(conversion)`). Results are kept for 6 hours, and anyone with
`conversion.download_url` can download the result until `conversion.expires_at`.

### Settings

`options` takes the API's conversion settings, the same as on the website. A few of them:

| Setting                                                          | For                                               |
| ---------------------------------------------------------------- | ------------------------------------------------- |
| `quality`: `"high"`, `"balanced"`, `"small"`                     | video, JPG, WebP, AVIF                            |
| `height`: `"1080"`, `"720"`, `"480"`, `"360"`                    | video (never upscaled)                            |
| `maxSize`: MB                                                    | MP4 that must fit a size                          |
| `maxKb`: KB                                                      | JPG, WebP, AVIF that must fit a size              |
| `longSide`, `resizeWidth`, `resizeHeight`, `fit`                 | resizing images                                   |
| `keepMetadata`, `keepLocation`: `True`                           | keep a photo's camera details and GPS (off)       |
| `bitrate`: `"64"`... `"320"`, `channels`, `trimStart`, `trimEnd` | audio                                             |
| `pageRange`: `"1-3, 5"`, `pdfA`: `True`                          | documents to PDF                                  |
| `ocr`: `True`, `ocrLanguage`                                     | scanned PDFs to Word or text                      |
| `compression`: `"light"`, `"balanced"`, `"strong"`               | PDF to a smaller PDF                              |
| `password`                                                       | a locked ZIP, 7Z or RAR (used once, never stored) |

The full list, and which settings apply to which conversion, is on the
[API page](https://converter.studio/developers). Settings that do not apply to a conversion
are ignored; a name the API does not know is refused (`BadRequestError`).

### Formats and account

```python
for fmt in client.formats():  # no key needed
    print(fmt.id, "->", ", ".join(fmt.targets))

heic = client.formats("heic")[0]  # what HEIC converts to
account = client.account()
print(account.plan, account.credits, account.renews_at)
```

### Price limits

A conversion goes ahead on its own when it costs at most `max_credits` (20 by default). A dearer one
waits for your go-ahead and costs nothing until then:

```python
from converter_studio import ConfirmationRequiredError

try:
    conversion = client.convert("lecture.mkv", "mp4", max_credits=5)
except ConfirmationRequiredError as quoted:
    print(f"It costs {quoted.credits} credits")
    conversion = client.wait(client.confirm(quoted.conversion))
```

`max_credits=0` only gets the price.

### Step by step

`convert()` is built from these, which you can call yourself:

```python
upload = client.upload("scan.pdf")  # large files go in parts
created = client.create_conversion("docx", upload_id=upload.id, options={"ocr": True})
created = client.create_conversion("jpg", url="https://example.com/photo.heic")
current = client.get_conversion(created.id)  # status now
current = client.get_conversion(created.id, wait=30)  # the API holds the answer up to 30 s
done = client.wait(created, timeout=900)
```

`wait()` returns a finished conversion or raises (see [Errors](#errors)). It lets the API hold
each status request until something changes, and pauses between answers that come back early (1
second, doubling up to 10).

### Callbacks and retries

```python
created = client.create_conversion(
    "mp4",
    url="https://example.com/raw/clip.mov",
    callback_url="https://example.com/hooks/converter-studio?token=SECRET",
    idempotency_key="clip-42",
)
```

- `callback_url`: an `https://` address on the public internet that Converter Studio POSTs to when
  the conversion waits for a go-ahead (`conversion.quoted`) and when it ends (`conversion.done`,
  `conversion.failed` or `conversion.canceled`). The body is
  `{"event", "id", "createdAt", "conversion"}`, with `Webhook-Id` and `Webhook-Timestamp` headers.
  Callbacks are not signed: keep the address secret, and check the conversion with
  `client.get_conversion(...)` before acting on one.
- `idempotency_key`: sending the same request again with the same key (within 24 hours) returns
  the first conversion (`conversion.replayed` is `True`) instead of starting and charging a second
  one; the same key with a different request raises `ConflictError`. The client makes up a key for
  each `create_conversion()` call and reuses it for its own retries, so pass your own when your
  code itself may repeat a request (a job queue retrying a task). With a local file, upload it
  once and repeat `create_conversion(upload_id=...)` with the same key.

### Async

```python
import asyncio
from converter_studio import AsyncConverterStudio


async def main() -> None:
    async with AsyncConverterStudio() as client:
        conversions = await asyncio.gather(
            *(client.convert(name, "webp") for name in ["a.png", "b.png", "c.png"])
        )
        for conversion in conversions:
            await client.download(conversion, "web/")


asyncio.run(main())
```

`AsyncConverterStudio` has the same methods as `ConverterStudio`, to be awaited;
`iter_download()` is an async iterator.

### Errors

Every exception derives from `ConverterStudioError`, and `str(error)` is a sentence meant for
people (for API errors, the API's own message).

| Exception                                                                                 | When                                                                                                         |
| ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `APIError`                                                                                | the API answered with an error: `.status_code`, `.code` (`notEnoughCredits`...), `.message`, `.params`       |
| `AuthenticationError` (401)                                                               | the API key does not work                                                                                    |
| `InsufficientCreditsError` (402)                                                          | not enough credits (`params`: `needed`, `balance`)                                                           |
| `FileTooLargeError` (413), `UnprocessableError` (422)                                     | the file is over the plan's limit; the file cannot be converted that way (`unsupportedConversion`)           |
| `BadRequestError`, `NotFoundError`, `ConflictError`, `GoneError`, `PermissionDeniedError` | 400, 404, 409, 410, 403                                                                                      |
| `RateLimitError` (429), `ServerError` (5xx)                                               | retried on their own first (`max_retries`, 2 by default); `.retry_after` in seconds                          |
| `ConversionFailedError`                                                                   | the conversion failed, expired or was canceled: `.code`, `.message`, `.conversion`; failures are not charged |
| `ConfirmationRequiredError`                                                               | the price is over `max_credits`: `.credits`, `.conversion`                                                   |
| `ConversionTimeoutError`                                                                  | not done within `timeout`; it may still finish (`.conversion.id`)                                            |
| `MissingApiKeyError`, `APIConnectionError`, `APITimeoutError`                             | no key; the API could not be reached                                                                         |

```python
from converter_studio import APIError, ConversionFailedError

try:
    client.convert("broken.heic", "jpg")
except ConversionFailedError as failed:
    print(failed.code, failed.message)
except APIError as error:
    print(error.status_code, error.code, error.message)
```

### Configuration

```python
import httpx
from converter_studio import ConverterStudio

client = ConverterStudio(
    api_key="cvs_live_...",  # default: CONVERTER_STUDIO_API_KEY
    base_url="https://converter.studio/api",  # default: CONVERTER_STUDIO_BASE_URL or this
    timeout=60.0,  # seconds for each network step
    max_retries=2,  # network errors, 408, 429, 5xx
    http_client=httpx.Client(proxy="http://proxy:3128"),  # your own client, not closed for you
    client_name="my-integration/1.2.0",  # only for packages built on this client, see below
)
```

The API key is only sent to `base_url`; upload and download links carry their own tokens.

A package built on this client (a LangChain toolkit, a Django app) passes its own name and version
as `client_name="<name>/<version>"`, in lower-case letters, digits and hyphens, then a version such
as `1.2.0`; anything else raises `ValueError`. Every request then sends
`X-Converter-Studio-Client: <name>/<version>` instead of `python/<version>`, and the User-Agent
starts with it. Both clients, `ConverterStudio` and `AsyncConverterStudio`, take it.

## Command line

The package installs `converter-studio` (also `python -m converter_studio`):

> **Two commands are named `converter-studio`.** This package's command uploads every file to
> converter.studio and converts it there, with your API key and credits. The command that
> [Converter Studio for desktop](https://converter.studio/download) installs converts on your
> computer and uses the cloud only with `--cloud`. If you have both, the one that comes first on
> your `PATH` runs; `converter-studio --help` tells them apart (the desktop app's begins with
> "Convert files on this computer"). `python -m converter_studio` always runs this package's
> command. To run the desktop app's, call it by its full path: `which -a converter-studio`
> (macOS, Linux) or `where.exe converter-studio` (Windows) lists every one, first one first.

```console
$ converter-studio convert talk.mov --to mp4 -O height=720 -O quality=balanced
[1/1] talk.mov -> mp4
  saved talk.mp4 (18.8 KB, 1 credit)
talk.mp4

$ converter-studio convert *.heic --to jpg --output-dir jpg/ -O keepMetadata=true
$ converter-studio convert https://example.com/report.docx --to pdf
$ converter-studio formats --from heic
$ converter-studio account
```

| Option                                  | What it does                                                                   |
| --------------------------------------- | ------------------------------------------------------------------------------ |
| `-t`, `--to FORMAT`                     | the target format (`jpg`, `mp4`, `pdf`...)                                     |
| `-o`, `--output-dir DIR`                | where results go (default: the current folder)                                 |
| `-O`, `--option KEY=VALUE`              | a conversion setting, repeated for more; `KEY:=JSON` sends a JSON value        |
| `--max-credits N`                       | the most a conversion may cost to start on its own (default 20)                |
| `--timeout SECONDS`                     | how long to wait for each conversion (default 600)                             |
| `--overwrite`                           | replace existing files (otherwise the result gets `-1`, `-2`...)               |
| `--json`                                | print one JSON object per file (or the formats, or the account) to stdout      |
| `-q`, `--quiet`                         | print only errors                                                              |
| `--api-key-file PATH`, `--base-url URL` | the key from a file instead of `CONVERTER_STUDIO_API_KEY`; another API address |

Progress goes to stderr and the saved paths to stdout, one per line, so the output can be piped.
The API key is never printed. Exit codes: `0` all done, `1` a conversion or request failed, `2`
wrong usage, `3` no API key or a key that does not work, `4` not enough credits or a price over
`--max-credits`, `5` a conversion did not finish within `--timeout`, `130` interrupted. With
several files, the code is the first failure's, and the other files are still converted (except
after `3` or a lack of credits).

## Limits

- The largest file depends on the plan (`client.account().max_file_bytes`). One request carries up
  to 95 MB; the client sends larger files in parts on its own.
- Audio and video length also depends on the plan (`max_media_seconds`).
- Results are kept for 6 hours, then deleted. A status request can wait up to 60 seconds.
- One file in, one file out: no merging or splitting of PDFs.

## Privacy

Your files are sent to Converter Studio (converter.studio) to be converted. Uploaded files are
deleted as soon as they are converted, and results 6 hours later. Each request also says which
client sent it (`X-Converter-Studio-Client: python/<version>`), for usage statistics only. See the
[privacy policy](https://converter.studio/privacy) and the [terms](https://converter.studio/terms).

## Links

- Converter Studio: <https://converter.studio>
- API documentation: <https://converter.studio/developers>
- Pricing: <https://converter.studio/pricing>
- Source code and issues: <https://github.com/tariksucu/converter-studio-python>
- Changelog: <https://github.com/tariksucu/converter-studio-python/blob/main/CHANGELOG.md>
- Support: <support@converter.studio>

MIT licensed. © Converter Studio.
