# Changelog

All notable changes to this package are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the package uses
[semantic versioning](https://semver.org/).

## 1.0.1 - unreleased

- README: a note in "Command line" on the two commands named `converter-studio`. This package's
  uploads every file to converter.studio; the desktop app's converts on the computer and uses the
  cloud only with `--cloud`. The one first on `PATH` runs; `python -m converter_studio` always
  runs this package's. No change in behaviour.

## 1.0.0 - 2026-11-23

The first release.

- `ConverterStudio` and `AsyncConverterStudio`: convert a local file, bytes, a file object or a
  link with any of the API's settings; wait with long polling and a back-off; download, stream or
  read the result; list formats; read the account's plan and credits.
- Uploads in parts for files over 95 MB, retries for network errors, 408, 429 and 5xx answers,
  an `Idempotency-Key` on every conversion, `callback_url`, and typed errors with the API's code
  and message.
- The `converter-studio` command: `convert` (several files, `--option`, `--output-dir`),
  `formats` and `account`, with exit codes for scripts.
- `client_name="<name>/<version>"` for packages built on the client: their own name in the
  `X-Converter-Studio-Client` header (instead of `python/<version>`) and in front of the
  User-Agent, checked against the header's rules.
