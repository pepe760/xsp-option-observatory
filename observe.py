"""Store source snapshots privately and derive research-only SPS observations."""
import argparse
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SETTINGS = json.loads((ROOT / "config/research.json").read_text())
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
MAX_PACKET = 16 * 1024 * 1024
KINDS = {"LIVE_BBO", "HISTORICAL_BBO", "DELAYED_BBO", "INDICATIVE", "OHLC"}
PUBLIC_NOTE = "公開頁顯示收集狀態。私人報價觀察可在瀏覽器本機載入。"
PRIVATE_NOTE = "本機私人資料。候選組合是報價觀察，未計費用或成交。"
PUBLIC_FIELDS = {
    "symbol", "target_delta", "wing", "status", "session", "expiry",
    "observed_at", "reason", "source",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def clock(value):
    if not isinstance(value, str):
        raise ValueError("expected an ISO timestamp with timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp missing timezone")
    return result.astimezone(UTC)


def day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("expected YYYY-MM-DD")
    result = date.fromisoformat(value)
    if value != result.isoformat():
        raise ValueError("expected YYYY-MM-DD")
    return result


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def nonnegative(value):
    return number(value) and value >= 0


def json_object(pairs):
    result = dict(pairs)
    if len(pairs) != len(result):
        raise ValueError("duplicate JSON key")
    return result


def reject_constant(value):
    raise ValueError(f"invalid JSON number: {value}")


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=json_object, parse_constant=reject_constant)


def validate_packet(raw):
    packet = parse_json(raw)
    if not isinstance(packet, dict) or type(packet.get("schema_version")) is not int or packet["schema_version"] != 1:
        raise ValueError("snapshot schema_version must be 1")
    symbol = packet.get("symbol")
    if symbol not in SETTINGS["widths"] or packet.get("source_kind") not in KINDS:
        raise ValueError("unrecognized symbol or source_kind")
    source = packet.get("source")
    if not isinstance(source, str) or not source or len(source) > 64:
        raise ValueError("source identifier is required")
    captured = clock(packet.get("captured_at_utc"))
    received = clock(packet.get("received_at_utc"))
    if captured > received or received > datetime.now(UTC) + timedelta(seconds=2):
        raise ValueError("capture/receipt timestamps are out of order or in the future")
    if "expiration_dates" in packet:
        dates = packet["expiration_dates"]
        if not isinstance(dates, list) or len(dates) > 1000:
            raise ValueError("invalid expiration_dates")
        for expiry in dates:
            day(expiry)
    underlying = packet.get("underlying")
    if underlying is not None and (not isinstance(underlying, dict) or underlying.get("symbol") != symbol):
        raise ValueError("underlying symbol mismatch")
    quotes = packet.get("quotes", [])
    if not isinstance(quotes, list) or len(quotes) > 50000:
        raise ValueError("invalid quotes array")
    if packet["source_kind"] == "OHLC" and quotes:
        raise ValueError("OHLC bars belong in bars, not quotes")
    seen = set()
    for quote in quotes:
        if not isinstance(quote, dict) or quote.get("symbol") != symbol:
            raise ValueError("option quote symbol mismatch")
        day(quote.get("expiry"))
        strike = quote.get("strike")
        if quote.get("right") not in {"P", "C"} or not number(strike) or strike <= 0:
            raise ValueError("invalid contract strike or right")
        for field in ("quote_ts", "greeks_ts", "greeks_underlying_ts"):
            if quote.get(field) is not None:
                clock(quote[field])
        trading_class = quote.get("trading_class")
        multiplier = quote.get("multiplier")
        if trading_class is not None and (not isinstance(trading_class, str) or len(trading_class) > 32):
            raise ValueError("invalid trading class")
        if multiplier is not None and (not number(multiplier) or multiplier <= 0):
            raise ValueError("invalid multiplier")
        key = (quote["expiry"], Decimal(str(strike)), quote["right"], trading_class, multiplier)
        if key in seen:
            raise ValueError("duplicate contract in snapshot")
        seen.add(key)
    if underlying and underlying.get("quote_ts") is not None:
        clock(underlying["quote_ts"])
    return packet, captured, received


def market_window(captured):
    raw = (ROOT / "config/calendar.json").read_bytes()
    if digest(raw) != SETTINGS["calendar_sha256"]:
        raise ValueError("calendar checksum mismatch")
    cal = json.loads(raw)
    ny = captured.astimezone(NY)
    session = ny.date()
    known_end = day(cal["known_end"])
    if not day(cal["known_start"]) <= session <= known_end:
        return session, None, "CALENDAR_UNKNOWN"
    holidays = {item for year in cal["holidays"].values() for item in year}
    if session.weekday() >= 5 or session.isoformat() in holidays:
        return session, None, "OUTSIDE_SESSION"
    half_days = set(cal["half_days"])
    close = time.fromisoformat(cal["half_day_close"] if session.isoformat() in half_days else cal["normal_close"])
    if not time.fromisoformat(cal["normal_open"]) <= ny.time() <= close:
        return session, None, "OUTSIDE_SESSION"
    next_day = session + timedelta(days=1)
    while next_day <= known_end and (next_day.weekday() >= 5 or next_day.isoformat() in holidays):
        next_day += timedelta(days=1)
    if next_day > known_end:
        return session, None, "CALENDAR_UNKNOWN"
    return session, next_day, None


def freshness(value, captured):
    if value is None:
        return False
    age = (captured - clock(value)).total_seconds()
    return 0 <= age <= SETTINGS["max_quote_age_seconds"]


def entry_quote(quote, captured):
    return (
        freshness(quote.get("quote_ts"), captured)
        and nonnegative(quote.get("bid"))
        and nonnegative(quote.get("ask"))
        and 0 <= quote["bid"] <= quote["ask"]
        and quote["ask"] > 0
    )


def standard_put(quote, symbol, expiry):
    return (
        quote["symbol"] == symbol
        and quote["expiry"] == expiry
        and quote["right"] == "P"
        and quote.get("trading_class") == symbol
        and quote.get("multiplier") == 100
        and quote.get("standard_contract") is True
    )


def usable_delta(quote, captured):
    delta = quote.get("delta")
    return (
        number(delta) and -1 <= delta < 0
        and quote.get("greeks_source") in {"VENDOR", "MODEL"}
        and freshness(quote.get("greeks_ts"), captured)
        and freshness(quote.get("greeks_underlying_ts"), captured)
        and number(quote.get("greeks_underlying_price"))
        and quote["greeks_underlying_price"] > 0
    )


def minimum_size(quote, side):
    size = quote.get(f"{side}_size")
    return number(size) and size >= SETTINGS["minimum_entry_size"]


def pair_observation(short, long, captured, underlying):
    if not all(entry_quote(leg, captured) for leg in (short, long)):
        return "QUOTE_UNQUALIFIED", {}
    if not minimum_size(short, "bid") or not minimum_size(long, "ask"):
        return "QUOTE_UNQUALIFIED", {}
    leg_clocks = [clock(leg["quote_ts"]) for leg in (short, long)]
    spot_time = clock(underlying["quote_ts"])
    clocks = leg_clocks + [spot_time, clock(short["greeks_ts"]), clock(short["greeks_underlying_ts"])]
    if abs(short["greeks_underlying_price"] / underlying["price"] - 1) > 0.001:
        return "QUOTE_UNQUALIFIED", {}
    if (max(clocks) - min(clocks)).total_seconds() > SETTINGS["max_leg_skew_seconds"]:
        return "QUOTE_UNQUALIFIED", {}
    credit = Decimal(str(short["bid"])) - Decimal(str(long["ask"]))
    width = Decimal(str(short["strike"])) - Decimal(str(long["strike"]))
    if credit <= 0:
        return "NONPOSITIVE_CREDIT", {}
    if credit >= width:
        return "QUOTE_UNQUALIFIED", {}
    debit = None
    if long["bid"] > 0 and minimum_size(short, "ask") and minimum_size(long, "bid"):
        debit = float(Decimal(str(short["ask"])) - Decimal(str(long["bid"])))
    return "CAPTURED", {
        "short_strike": short["strike"],
        "long_strike": long["strike"],
        "actual_short_delta": short["delta"],
        "greeks_source": short["greeks_source"],
        "natural_credit": float(credit),
        "natural_debit": debit,
        "max_loss_per_spread_before_fees": float((width - credit) * 100),
    }


def derive(packet, captured, received):
    symbol = packet["symbol"]
    kind = packet["source_kind"]
    session, next_day, problem = market_window(captured)
    expiry = next_day.isoformat() if next_day else None
    underlying = packet.get("underlying") or {}
    available = packet.get("expiration_dates")
    quotes = [q for q in packet.get("quotes", []) if standard_put(q, symbol, expiry)]
    spot = underlying.get("price")
    if kind == "OHLC":
        problem = "OHLC_ONLY"
    elif kind in {"INDICATIVE", "DELAYED_BBO"}:
        problem = "INDICATIVE_ONLY"
    elif problem is None:
        if available is not None and expiry not in available and packet.get("expiry_inventory_complete") is True:
            problem = "EXPIRY_UNVERIFIED" if any(q["expiry"] == expiry for q in packet.get("quotes", [])) else "NO_NEXT_SESSION_EXPIRY"
        elif not quotes:
            has_expiry = (available is not None and expiry in available) or any(
                q["expiry"] == expiry for q in packet.get("quotes", [])
            )
            problem = "QUOTE_UNQUALIFIED" if has_expiry else "EXPIRY_UNVERIFIED"
        elif not (
            number(spot) and spot > 0
            and isinstance(underlying.get("source"), str) and underlying["source"].strip()
            and freshness(underlying.get("quote_ts"), captured)
        ):
            problem = "QUOTE_UNQUALIFIED"
        elif kind == "LIVE_BBO" and (received - captured).total_seconds() > SETTINGS["max_live_receipt_lag_seconds"]:
            problem = "QUOTE_UNQUALIFIED"
    by_strike = {Decimal(str(q["strike"])): q for q in quotes}
    for target in SETTINGS["target_deltas"]:
        selected = None
        base_status = problem
        if base_status is None:
            otm = [q for q in quotes if q["strike"] < spot]
            valid = [q for q in otm if usable_delta(q, captured)]
            band = min(Decimal(".02"), Decimal(str(target)) / 2)
            goal = Decimal(str(target))
            eligible = [q for q in valid if abs(abs(Decimal(str(q["delta"]))) - goal) <= band]
            if eligible:
                selected = min(eligible, key=lambda q: (abs(abs(Decimal(str(q["delta"]))) - goal), q["strike"]))
            else:
                base_status = "QUOTE_UNQUALIFIED" if any(not usable_delta(q, captured) for q in otm) else "DELTA_UNREACHABLE"
        for wing in SETTINGS["widths"][symbol]:
            status, values = base_status, {}
            if selected is not None:
                long = by_strike.get(Decimal(str(selected["strike"])) - Decimal(str(wing)))
                if long is None:
                    status = "MISSING_EXACT_WING"
                else:
                    status, values = pair_observation(selected, long, captured, underlying)
            yield {
                "symbol": symbol, "target_delta": target, "wing": wing,
                "session": session.isoformat(), "expiry": expiry, "observed_at": captured.isoformat(),
                "status": status, "reason": status, "source": packet["source"],
                "source_kind": kind, "dte": (next_day - session).days if next_day else None,
                **values,
            }


def private_path(path):
    resolved = Path(path).expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("private archive and exports must be outside the checkout")
    if resolved.exists() and (not resolved.is_file() or resolved.stat().st_nlink != 1):
        raise ValueError("private path must be an ordinary, single-link file")
    return resolved


def database(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("ab"):
        pass
    path.chmod(0o600)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS policy (key TEXT PRIMARY KEY, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_kind TEXT NOT NULL,
          symbol TEXT NOT NULL, captured_at TEXT NOT NULL, received_at TEXT NOT NULL,
          imported_at TEXT NOT NULL, sha256 TEXT NOT NULL, payload_gzip BLOB NOT NULL,
          UNIQUE(source, source_kind, symbol, captured_at)
        );
        CREATE TABLE IF NOT EXISTS observations (
          snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
          target_delta REAL NOT NULL, wing REAL NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(snapshot_id, target_delta, wing)
        );
        CREATE INDEX IF NOT EXISTS snapshots_by_symbol_clock
          ON snapshots(symbol, captured_at DESC);
    """)
    return db


def ingest(db_path, source):
    inserted = 0
    with closing(database(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        policy_digest = digest(encode(SETTINGS) + Path(__file__).read_bytes())
        db.execute("INSERT OR IGNORE INTO policy VALUES ('selection',?)", (policy_digest,))
        if db.execute("SELECT digest FROM policy WHERE key='selection'").fetchone()[0] != policy_digest:
            raise ValueError("selection version changed; start a separate archive")
        while raw := source.readline(MAX_PACKET + 1):
            if len(raw) > MAX_PACKET:
                raise ValueError("snapshot exceeds 16 MiB")
            raw = raw.strip()
            if not raw:
                continue
            packet, captured, received = validate_packet(raw)
            key = (packet["source"], packet["source_kind"], packet["symbol"], captured.isoformat())
            existing = db.execute(
                "SELECT sha256 FROM snapshots WHERE source=? AND source_kind=? AND symbol=? AND captured_at=?",
                key,
            ).fetchone()
            sha256 = digest(raw)
            if existing:
                if existing[0] != sha256:
                    raise ValueError("conflicting snapshot identity; original retained")
                continue
            snapshot = db.execute(
                """INSERT INTO snapshots(source,source_kind,symbol,captured_at,received_at,imported_at,sha256,payload_gzip)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (*key, received.isoformat(), datetime.now(UTC).isoformat(), sha256, gzip.compress(raw, mtime=0)),
            ).lastrowid
            db.executemany(
                "INSERT INTO observations VALUES (?,?,?,?)",
                ((snapshot, row["target_delta"], row["wing"], encode(row).decode())
                 for row in derive(packet, captured, received)),
            )
            inserted += 1
    return inserted


def waiting_rows():
    for symbol, widths in SETTINGS["widths"].items():
        for target in SETTINGS["target_deltas"]:
            for wing in widths:
                yield dict(
                    symbol=symbol, target_delta=target, wing=wing, session=None, expiry=None,
                    observed_at=None, source=None, status="WAITING_FOR_SOURCE", reason="WAITING_FOR_SOURCE",
                )


def public_status():
    return dict(schema_version=1, mode="STATUS_ONLY", generated_at=None,
                rows=list(waiting_rows()), note=PUBLIC_NOTE)


def validate_public(path):
    expected = public_status()
    result = parse_json(Path(path).read_bytes())
    if result != expected or any(set(row) != PUBLIC_FIELDS for row in result["rows"]):
        raise ValueError("public status must match the generated static configuration exactly")


def private_view(db_path, limit):
    if not db_path.exists():
        raise ValueError("private archive does not exist")
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        results = db.execute(
            """SELECT o.payload FROM observations o JOIN snapshots s ON s.id=o.snapshot_id
               ORDER BY s.captured_at DESC,s.id DESC,o.target_delta,o.wing LIMIT ?""", (limit,),
        )
        rows = [json.loads(row[0]) for row in results]
    return dict(schema_version=1, mode="PRIVATE_LOCAL", generated_at=datetime.now(UTC).isoformat(),
                rows=rows, note=PRIVATE_NOTE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("ingest", help="Archive normalized NDJSON privately")
    collect.add_argument("--db", required=True)
    collect.add_argument("--input", default="-", help="NDJSON path or stdin")
    collect.add_argument("--confirm-storage-rights", action="store_true", required=True)
    export = commands.add_parser("export-private", help="Write a browser-local observation file")
    export.add_argument("--db", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--limit", type=int, default=3000, choices=range(1, 5001), metavar="1..5000")
    commands.add_parser("public-status", help="Emit configuration only, without accessing the archive")
    check = commands.add_parser("validate-public", help="Reject non-public data at build time")
    check.add_argument("path")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "ingest":
            db_path = private_path(args.db)
            if args.input == "-":
                count = ingest(db_path, sys.stdin.buffer)
            else:
                with Path(args.input).expanduser().open("rb") as source:
                    count = ingest(db_path, source)
            print(json.dumps({"inserted_snapshots": count}))
        elif args.command == "export-private":
            view = private_view(private_path(args.db), args.limit)
            output = private_path(args.output)
            data = encode(view)
            if len(data) > 4 * 1024 * 1024:
                raise ValueError("private export exceeds 4 MiB; use a smaller --limit")
            with output.open("xb") as file:
                file.write(data + b"\n")
        elif args.command == "public-status":
            sys.stdout.buffer.write(encode(public_status()) + b"\n")
        else:
            validate_public(args.path)
    except (ValueError, TypeError, OverflowError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Collector error: {error}\n")


if __name__ == "__main__":
    main()
