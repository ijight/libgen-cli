# libgen-cli

A lightweight, fast Python CLI for searching and downloading books from arbitrary
[Library Genesis](https://en.wikipedia.org/wiki/Library_Genesis) mirrors.

- Pure LibGen — uses the `libgen.li` family of mirrors (`libgen.li`, `libgen.gl`,
  `libgen.vg`, `libgen.la`, `libgen.bz`).
- Non-fiction + Fiction in one search.
- Automatic mirror health probe and ranked failover.
- MD5-verified, resumable, atomically-written downloads.
- Pipe-friendly NDJSON output for bulk scripting.
- Optional interactive multi-select picker.

## Install

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
uv run libgen --help
```

## Quick start

```bash
# Search non-fiction + fiction (default), pretty table on TTY
libgen search "category theory"

# Limit results, narrow to non-fiction, emit NDJSON
libgen search "rust programming" --topic nonfic -n 50 --json

# Filter by extension, language, and year (any combination)
libgen search "the hobbit" --ext epub,pdf --lang en --year 2010-
libgen search "tolkien" -e epub -l english -y 2020

# Download by MD5
libgen download abc123def456...

# Bulk download from a file of MD5s
libgen download --bulk md5s.txt -j 8

# Pipe NDJSON into the downloader
libgen search "category theory" --json \
  | jq 'select(.ext == "pdf")' \
  | libgen download --from-stdin -j 4

# Resolve direct URL only (no download)
libgen link abc123def456...

# Interactive multi-select picker
libgen pick "donald knuth"

# Probe mirror health and persist ranking
libgen mirrors --probe
```

## Subcommands

| Command | Purpose |
| --- | --- |
| `libgen search QUERY` | Search across non-fiction + fiction (configurable; supports `--ext`, `--lang`, `--year`). |
| `libgen download MD5...` | Download one or more books by MD5. |
| `libgen download --bulk FILE` | Download every MD5 listed in `FILE` (one per line). |
| `libgen download --from-stdin` | Read NDJSON book records or plain MD5s from stdin. |
| `libgen link MD5` | Resolve and print the direct URL without downloading. |
| `libgen pick QUERY` | Search (with the same `--ext` / `--lang` / `--year` filters), then interactively pick rows to download. |
| `libgen mirrors [--probe]` | List mirrors with health and latency. |

## Filtering search results

`libgen search` and `libgen pick` accept three client-side filters that compose
freely (server-side `ext:` modifiers don't actually narrow results on
libgen.li-family mirrors, so all filtering happens after parsing):

| Flag | Purpose | Examples |
| --- | --- | --- |
| `-e, --ext` | File extension(s); repeat or comma-separate | `--ext epub`, `-e epub,pdf` |
| `-l, --lang` | Language(s); case-insensitive prefix match | `--lang en`, `-l english,french` |
| `-y, --year` | Single year or inclusive range | `--year 2020`, `-y 2010-2020`, `-y 2010-`, `-y -2020` |

Equivalent shell-pipeline filtering with `jq`:

```bash
libgen search "the hobbit" --json \
  | jq -c 'select(.extension == "epub" and .language == "English")'
```

## Configuration

Defaults live in `~/.config/libgen-cli/config.toml` (XDG-respecting).
Override the mirror set via `--mirror URL` (repeatable) or `LIBGEN_MIRROR=...`.

## Development

```bash
uv sync
uv run libgen --help

# lint + format
uv run ruff check
uv run ruff format --check

# type-check
uv run mypy src

# tests
uv run pytest
```

All tests are hermetic — no live network calls. HTTP traffic is mocked with
`respx` against committed HTML fixtures.

## License

MIT.
