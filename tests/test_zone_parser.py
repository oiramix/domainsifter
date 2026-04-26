"""Unit tests for scripts/zone_parser.py — operates on synthetic gzipped zones."""

from __future__ import annotations

import gzip

import pytest

from scripts import zone_parser


def _write_gz(path, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)


def test_parse_zone_extracts_apex_names_and_lowercases(tmp_path):
    zone = tmp_path / "example.zone.gz"
    _write_gz(
        zone,
        "Example.com. 3600 IN NS ns1.registrar.com.\n"
        "EXAMPLE.COM. 3600 IN DS 12345 8 2 ABCD\n"
        "foo.example.com. 3600 IN A 1.2.3.4\n"
        "another.com. 3600 IN NS ns1.example.net.\n",
    )
    result = zone_parser.parse_zone(zone)
    assert result == {"example.com", "foo.example.com", "another.com"}


def test_parse_zone_skips_blank_lines_comments_and_directives(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(
        zone,
        "$ORIGIN com.\n"
        "$TTL 3600\n"
        "; this is a comment\n"
        "\n"
        "   \n"
        "valid.com. 3600 IN NS ns1.foo.com.\n",
    )
    assert zone_parser.parse_zone(zone) == {"valid.com"}


def test_parse_zone_strips_trailing_dot(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(zone, "trailing.org. 3600 IN NS ns.x.com.\n")
    assert zone_parser.parse_zone(zone) == {"trailing.org"}


def test_parse_zone_dedupes_across_record_types(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(
        zone,
        "dup.app. 3600 IN NS ns1.a.com.\n"
        "dup.app. 3600 IN NS ns2.a.com.\n"
        "dup.app. 3600 IN DS 1 8 2 ABCD\n"
        "dup.app. 3600 IN RRSIG NS 8 2 3600 ...\n",
    )
    assert zone_parser.parse_zone(zone) == {"dup.app"}


def test_parse_zone_handles_tab_separated(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(zone, "tabbed.dev.\t3600\tIN\tNS\tns.foo.com.\n")
    assert zone_parser.parse_zone(zone) == {"tabbed.dev"}


def test_parse_zone_handles_empty_zone(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(zone, "")
    assert zone_parser.parse_zone(zone) == set()


def test_parse_zone_handles_only_directives_and_comments(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(zone, "$ORIGIN xyz.\n$TTL 3600\n; nothing else\n")
    assert zone_parser.parse_zone(zone) == set()


def test_iter_apex_names_yields_duplicates(tmp_path):
    zone = tmp_path / "z.gz"
    _write_gz(
        zone,
        "a.com. 3600 IN NS ns1.\n"
        "a.com. 3600 IN DS 1 8 2 ABCD\n"
        "b.com. 3600 IN NS ns1.\n",
    )
    names = list(zone_parser.iter_apex_names(zone))
    assert names == ["a.com", "a.com", "b.com"]


def test_parse_zone_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        zone_parser.parse_zone(tmp_path / "missing.gz")
