"""Download tests: URL resolution, streaming, MD5 verification, failover."""

from __future__ import annotations

import hashlib
from pathlib import Path

import respx

from libgen_cli.download import (
    download_book,
    download_many,
    resolve_download_url,
)
from libgen_cli.http import make_client
from libgen_cli.models import Book, Topic

PAYLOAD = b"hello world libgen test payload"
PAYLOAD_MD5 = hashlib.md5(PAYLOAD).hexdigest()


def _book_for(md5: str, title: str = "Sample") -> Book:
    return Book(md5=md5, title=title, extension="bin", topic=Topic.NONFIC)


@respx.mock
def test_resolve_direct_path_succeeds() -> None:
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200,
        headers={"content-type": "application/octet-stream"},
    )
    with make_client() as client:
        resolved = resolve_download_url(client, "https://libgen.li", PAYLOAD_MD5)
    assert resolved is not None
    assert resolved.via_key is False
    assert resolved.mirror == "https://libgen.li"


@respx.mock
def test_resolve_falls_back_to_keyed_link(book_page_html: str) -> None:
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200, headers={"content-type": "text/html"}
    )
    respx.get(f"https://libgen.li/ads.php?md5={PAYLOAD_MD5}").respond(
        200,
        text=book_page_html.replace("44a7f3a19a9cd60fc54a1d9322f38120", PAYLOAD_MD5),
    )
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}&key=H8TFHSC5ISZOIP2Y").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    with make_client() as client:
        resolved = resolve_download_url(client, "https://libgen.li", PAYLOAD_MD5)
    assert resolved is not None
    assert resolved.via_key is True


@respx.mock
def test_resolve_returns_none_when_no_keyed_link() -> None:
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200, headers={"content-type": "text/html"}
    )
    respx.get(f"https://libgen.li/ads.php?md5={PAYLOAD_MD5}").respond(
        200, text="<html>no keys here</html>"
    )
    with make_client() as client:
        assert resolve_download_url(client, "https://libgen.li", PAYLOAD_MD5) is None


@respx.mock
def test_download_book_streams_and_verifies_md5(tmp_path: Path) -> None:
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    respx.get(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200,
        content=PAYLOAD,
        headers={
            "content-type": "application/octet-stream",
            "content-length": str(len(PAYLOAD)),
        },
    )
    book = _book_for(PAYLOAD_MD5)
    with make_client() as client:
        result = download_book(client, book, ["https://libgen.li"], out_dir=tmp_path)
    assert result.success is True
    assert result.bytes_written == len(PAYLOAD)
    saved = Path(result.path)  # type: ignore[arg-type]
    assert saved.exists()
    assert saved.read_bytes() == PAYLOAD


@respx.mock
def test_download_book_md5_mismatch_falls_over(tmp_path: Path) -> None:
    bogus = b"not the right bytes"

    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    respx.get(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200, content=bogus, headers={"content-type": "application/octet-stream"}
    )

    respx.head(f"https://libgen.la/get.php?md5={PAYLOAD_MD5}").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    respx.get(f"https://libgen.la/get.php?md5={PAYLOAD_MD5}").respond(
        200, content=PAYLOAD, headers={"content-type": "application/octet-stream"}
    )

    book = _book_for(PAYLOAD_MD5)
    with make_client() as client:
        result = download_book(
            client,
            book,
            ["https://libgen.li", "https://libgen.la"],
            out_dir=tmp_path,
        )
    assert result.success is True
    assert result.mirror_used == "https://libgen.la"
    saved = Path(result.path)  # type: ignore[arg-type]
    assert saved.read_bytes() == PAYLOAD
    leftover_part = list(tmp_path.glob("*.part"))
    assert leftover_part == [], f"part files lingered: {leftover_part}"


@respx.mock
def test_download_book_all_mirrors_fail(tmp_path: Path) -> None:
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(503)
    respx.get(f"https://libgen.li/ads.php?md5={PAYLOAD_MD5}").respond(503)
    respx.head(f"https://libgen.la/get.php?md5={PAYLOAD_MD5}").respond(503)
    respx.get(f"https://libgen.la/ads.php?md5={PAYLOAD_MD5}").respond(503)

    book = _book_for(PAYLOAD_MD5)
    with make_client() as client:
        result = download_book(
            client,
            book,
            ["https://libgen.li", "https://libgen.la"],
            out_dir=tmp_path,
        )
    assert result.success is False
    assert result.error is not None


@respx.mock
def test_download_book_skips_when_file_exists(tmp_path: Path) -> None:
    book = _book_for(PAYLOAD_MD5, title="Existing")
    from libgen_cli.naming import filename_for

    final = tmp_path / filename_for(book)
    final.write_bytes(b"already here")

    with make_client() as client:
        result = download_book(client, book, ["https://libgen.li"], out_dir=tmp_path)
    assert result.skipped is True
    assert result.success is True
    assert final.read_bytes() == b"already here"


@respx.mock
def test_download_book_dry_run_does_not_write(tmp_path: Path) -> None:
    respx.head(f"https://libgen.li/get.php?md5={PAYLOAD_MD5}").respond(
        200, headers={"content-type": "application/octet-stream"}
    )
    book = _book_for(PAYLOAD_MD5)
    with make_client() as client:
        result = download_book(
            client,
            book,
            ["https://libgen.li"],
            out_dir=tmp_path,
            dry_run=True,
        )
    assert result.success is True
    assert "resolved_url" in result.extra
    assert list(tmp_path.glob("*.bin")) == []


@respx.mock
def test_download_many_returns_in_input_order(tmp_path: Path) -> None:
    md5s = [PAYLOAD_MD5, hashlib.md5(b"second").hexdigest()]
    payloads = {md5s[0]: PAYLOAD, md5s[1]: b"second"}

    for md5, body in payloads.items():
        respx.head(f"https://libgen.li/get.php?md5={md5}").respond(
            200, headers={"content-type": "application/octet-stream"}
        )
        respx.get(f"https://libgen.li/get.php?md5={md5}").respond(
            200, content=body, headers={"content-type": "application/octet-stream"}
        )

    books = [_book_for(m, title=f"book-{m[:6]}") for m in md5s]
    with make_client() as client:
        results = download_many(
            client, books, ["https://libgen.li"], out_dir=tmp_path, concurrency=2
        )
    assert [r.md5 for r in results] == md5s
    assert all(r.success for r in results)
