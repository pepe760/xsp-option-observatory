#!/usr/bin/env python3
"""Check locally available ThetaData XSP OHLC shards against the Drive manifest."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import re
import zlib
from datetime import date, datetime
from itertools import islice
from pathlib import Path


PREFIX = "xsp_ohlc_15m_atm25_2020_to_2026"
COLUMNS = (
    "trade_date", "dte_bucket", "actual_dte", "bar_et", "underlying_est",
    "atm_strike", "moneyness", "symbol", "expiration", "strike", "right",
    "timestamp", "open", "high", "low", "close", "volume", "count", "vwap",
)


def read_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("prefix") != PREFIX:
        raise ValueError("Expected the XSP 15-minute OHLC shard manifest")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Manifest requires a nonempty parts list")

    indexed = {}
    for part in parts:
        name = part.get("name") if isinstance(part, dict) else None
        if not isinstance(name, str) or not re.fullmatch(rf"{PREFIX}\.part[0-9]{{3}}\.csv\.gz", name):
            raise ValueError("Invalid shard name in manifest")
        if name in indexed:
            raise ValueError(f"Duplicate shard in manifest: {name}")
        for field in ("rows", "bytes"):
            value = part.get(field)
            if type(value) is not int or value < 1:
                raise ValueError(f"Invalid declared {field} for {name}")
        digest = part.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError(f"Invalid SHA-256 for {name}")
        indexed[name] = part

    total_rows = manifest.get("total_rows")
    if type(total_rows) is not int or total_rows != sum(part["rows"] for part in parts):
        raise ValueError("Manifest total_rows disagrees with its part counts")
    return indexed, total_rows


def inspect_sample(compressed, expected_rows):
    with gzip.GzipFile(fileobj=compressed) as uncompressed:
        with io.TextIOWrapper(uncompressed, encoding="utf-8-sig", newline="") as text:
            rows = csv.reader(text, strict=True)
            if tuple(next(rows, [])) != COLUMNS:
                return {"status": "UNEXPECTED_HEADER"}

            dates = set()
            timestamps = set()
            observed = malformed = 0
            date_column = COLUMNS.index("trade_date")
            timestamp_column = COLUMNS.index("timestamp")
            symbol_column = COLUMNS.index("symbol")
            for row in islice(rows, expected_rows):
                observed += 1
                if len(row) != len(COLUMNS) or row[symbol_column] != "XSP":
                    malformed += 1
                    continue
                try:
                    trade_date = date.fromisoformat(row[date_column])
                    timestamp_text = row[timestamp_column]
                    if len(timestamp_text) < 19 or timestamp_text[10] != "T":
                        raise ValueError("Expected ISO datetime with time of day")
                    timestamp = datetime.fromisoformat(timestamp_text)
                    if timestamp.tzinfo is not None or timestamp.date() != trade_date:
                        raise ValueError("Unexpected timestamp format or date")
                except ValueError:
                    malformed += 1
                    continue
                dates.add(trade_date)
                timestamps.add(timestamp)

    return {
        "status": "PASS" if observed == expected_rows and not malformed else "INVALID_SAMPLE",
        "header": list(COLUMNS),
        "rows_examined": observed,
        "malformed_identity_or_time_rows": malformed,
        "distinct_trade_dates": len(dates),
        "trade_date_min": min(dates).isoformat() if dates else None,
        "trade_date_max": max(dates).isoformat() if dates else None,
        "timestamp_min": min(timestamps).isoformat() if timestamps else None,
        "timestamp_max": max(timestamps).isoformat() if timestamps else None,
        "timestamp_clock": "as stored, timezone offset absent",
    }


def inspect_part(directory, part, sample_rows):
    name = part["name"]
    result = {
        "name": name,
        "manifest_rows": part["rows"],
        "manifest_bytes": part["bytes"],
        "sample": {"status": "NOT_INSPECTED"},
    }
    try:
        with (directory / name).open("rb") as compressed:
            before = os.fstat(compressed.fileno())
            digest = hashlib.sha256()
            observed_bytes = 0
            for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
                digest.update(chunk)
                observed_bytes += len(chunk)
            result["observed_bytes"] = observed_bytes
            result["size_matches"] = observed_bytes == part["bytes"]
            result["sha256_matches"] = digest.hexdigest() == part["sha256"].lower()
            if not (result["size_matches"] and result["sha256_matches"]):
                result["compressed_integrity"] = "MISMATCH"
                return result

            result["compressed_integrity"] = "PASS"
            compressed.seek(0)
            try:
                result["sample"] = inspect_sample(compressed, min(sample_rows, part["rows"]))
            except (OSError, EOFError, UnicodeError, csv.Error, zlib.error):
                result["sample"] = {"status": "UNREADABLE"}
            after = os.fstat(compressed.fileno())
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                result["compressed_integrity"] = "CHANGED_DURING_READ"
                result["sample"] = {"status": "NOT_INSPECTED"}
    except FileNotFoundError:
        result["compressed_integrity"] = "MISSING"
    except OSError:
        result["compressed_integrity"] = "UNREADABLE"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="local Drive shard manifest JSON")
    parser.add_argument("--archive-dir", type=Path, help="local shard directory, defaults to manifest's directory")
    parser.add_argument("--part", action="append", help="exact shard filename, repeat to select several")
    parser.add_argument("--sample-rows", type=int, default=1000, help="leading records per verified shard (1–10000)")
    args = parser.parse_args()
    if not 1 <= args.sample_rows <= 10000:
        parser.error("--sample-rows must be between 1 and 10000")
    try:
        manifest, total_rows = read_manifest(args.manifest)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    selected = set(args.part or manifest)
    if selected.difference(manifest):
        parser.error("--part must name a shard from the manifest")
    directory = args.archive_dir or args.manifest.parent
    parts = [inspect_part(directory, part, args.sample_rows) for name, part in manifest.items() if name in selected]
    passed = all(part["compressed_integrity"] == "PASS" and part["sample"]["status"] == "PASS" for part in parts)
    print(json.dumps({
        "classification": "OHLC_ONLY",
        "execution_quote_eligible": False,
        "manifest_rows": total_rows,
        "manifest_part_count": len(manifest),
        "full_archive_row_coverage_verified": False,
        "all_manifest_parts_selected": len(parts) == len(manifest),
        "selected_parts_pass": passed,
        "parts": parts,
    }, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
