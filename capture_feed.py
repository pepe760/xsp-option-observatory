"""Read-only inputs for the private options observatory. Market packets go to stdout."""

import argparse
import http.client
import json
import math
import socket
import sys
from datetime import date, datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


SYMBOLS = ("XSP", "SPY", "QQQ", "TLT", "SOXX")
THETA_HOST = "127.0.0.1"
THETA_PORT = 25503
NEW_YORK = ZoneInfo("America/New_York")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HANDOFF_BYTES = 32 * 1024 * 1024


class CaptureError(ValueError):
    """A feed input cannot be captured safely."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value, *, theta_local=False):
    if not isinstance(value, str) or "T" not in value:
        raise CaptureError("Expected an ISO 8601 datetime")
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise CaptureError("Invalid ISO 8601 datetime") from None
    if stamp.tzinfo is None:
        if not theta_local:
            raise CaptureError("Handoff timestamps must include a timezone offset")
        early = stamp.replace(tzinfo=NEW_YORK, fold=0)
        late = stamp.replace(tzinfo=NEW_YORK, fold=1)
        if early.utcoffset() != late.utcoffset():
            raise CaptureError("Ambiguous or nonexistent ThetaData New York timestamp")
        stamp = early
    try:
        return stamp.astimezone(timezone.utc).isoformat()
    except OverflowError:
        raise CaptureError("Timestamp is outside the supported UTC range") from None


def port_status(port):
    try:
        with socket.create_connection((THETA_HOST, port), timeout=0.3):
            return "TCP_LISTENER_ONLY"
    except ConnectionRefusedError:
        return "NO_LISTENER"
    except OSError:
        return "TCP_PROBE_FAILED"


def status():
    return {
        "checked_at_utc": utc_now(),
        "symbols": SYMBOLS,
        "localhost_tcp": {
            "theta_v3_rest_25503": port_status(THETA_PORT),
            "theta_v3_stream_25520": port_status(25520),
            "ibkr_tws_live_7496": port_status(7496),
            "ibkr_tws_paper_7497": port_status(7497),
            "ibkr_gateway_live_4001": port_status(4001),
            "ibkr_gateway_paper_4002": port_status(4002),
        },
        "market_data_entitlement": "UNVERIFIED",
        "automated_retrieval_and_local_retention_rights": "UNVERIFIED",
        "market_requests": 0,
    }


def validated_handoff(handle):
    from observe import MAX_PACKET, validate_packet

    payload = handle.read(MAX_HANDOFF_BYTES + 1)
    if len(payload) > MAX_HANDOFF_BYTES:
        raise CaptureError("Handoff exceeds 32 MiB; split it into batches")
    lines = []
    for number, line in enumerate(payload.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line) + 1 > MAX_PACKET:
            raise CaptureError(f"Handoff line {number} exceeds the snapshot size bound")
        try:
            packet, _, _ = validate_packet(line)
            if "quotes" not in packet:
                raise ValueError("required quotes array is missing")
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise CaptureError(f"Handoff line {number} fails the archive snapshot schema") from None
        lines.append(line)
    if not lines:
        raise CaptureError("Handoff contains zero packets")
    return b"\n".join(lines) + b"\n"


def unique_json_keys(pairs):
    item = {}
    for key, value in pairs:
        if key in item:
            raise CaptureError("Duplicate JSON object field")
        item[key] = value
    return item


def theta_rows(endpoint, params):
    # Using a literal loopback HTTP connection avoids proxy configuration and redirects.
    request = endpoint + "?" + urlencode({**params, "format": "ndjson", "current_day_only": "true"})
    connection = http.client.HTTPConnection(THETA_HOST, THETA_PORT, timeout=10)
    try:
        connection.request("GET", request)
        response = connection.getresponse()
        if response.status in (204, 472):
            return []
        if response.status != 200:
            raise CaptureError(f"ThetaData {endpoint} returned HTTP {response.status}")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise CaptureError(f"ThetaData {endpoint} unavailable ({type(error).__name__})") from None
    finally:
        connection.close()
    if len(body) > MAX_RESPONSE_BYTES:
        raise CaptureError(f"ThetaData {endpoint} response exceeds capture bound")
    try:
        rows = [json.loads(line, object_pairs_hook=unique_json_keys) for line in body.splitlines() if line.strip()]
    except (ValueError, RecursionError):
        raise CaptureError(f"ThetaData {endpoint} returned invalid NDJSON") from None
    if not all(isinstance(row, dict) for row in rows):
        raise CaptureError(f"ThetaData {endpoint} returned an unexpected row shape")
    return rows


def numeric(value):
    if type(value) not in (float, int):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def theta_contract_rows(rows, symbol, expiration, row_limit):
    if len(rows) > row_limit:
        raise CaptureError("ThetaData returned more contracts than the requested strike window")
    indexed = {}
    for row in rows:
        strike = numeric(row.get("strike"))
        right = {"call": "C", "put": "P", "c": "C", "p": "P"}.get(str(row.get("right", "")).lower())
        if row.get("symbol") != symbol or row.get("expiration") != expiration:
            raise CaptureError("ThetaData returned a different symbol or expiration")
        if strike is None or strike <= 0 or right is None:
            raise CaptureError("ThetaData returned an invalid strike or right")
        key = (strike, right)
        if key in indexed:
            raise CaptureError("ThetaData returned duplicate contract keys")
        indexed[key] = row
    return indexed


def theta_snapshot(symbol, expiration, strike_range, source_kind, with_greeks):
    spot_endpoint = "/v3/index/snapshot/price" if symbol == "XSP" else "/v3/stock/snapshot/trade"
    spot = theta_rows(spot_endpoint, {"symbol": symbol})
    if len(spot) != 1 or spot[0].get("symbol") != symbol:
        raise CaptureError(f"ThetaData did not return exactly one underlying observation for {symbol}")
    spot_price = numeric(spot[0].get("price"))
    if spot_price is None or spot_price <= 0:
        raise CaptureError(f"ThetaData did not return a positive underlying price for {symbol}")

    params = {"symbol": symbol, "expiration": expiration, "strike_range": strike_range, "right": "both"}
    max_rows = 2 * (2 * strike_range + 1)
    bbo = theta_contract_rows(theta_rows("/v3/option/snapshot/quote", params), symbol, expiration, max_rows)
    captured = datetime.now(timezone.utc)
    capture_date = captured.astimezone(NEW_YORK).date()
    if not bbo:
        raise CaptureError(f"ThetaData returned zero option quotes for {symbol} {expiration}")
    spot_ts = theta_same_day_time(spot[0].get("timestamp"), captured, capture_date)

    greeks = {}
    if with_greeks:
        rows = theta_rows("/v3/option/snapshot/greeks/all", params)
        greeks = theta_contract_rows(rows, symbol, expiration, max_rows)
    received = utc_now()
    quotes = []
    for (strike, right), row in bbo.items():
        quote_ts = theta_same_day_time(row.get("timestamp"), captured, capture_date)
        quote = {
            "symbol": symbol, "expiry": expiration, "strike": strike, "right": right,
            "quote_ts": quote_ts, "bid": numeric(row.get("bid")), "ask": numeric(row.get("ask")),
            "bid_size": numeric(row.get("bid_size")), "ask_size": numeric(row.get("ask_size")),
            "delta": None, "gamma": None, "iv": None, "greeks_ts": None, "greeks_source": None,
            "trading_class": None, "multiplier": None, "standard_contract": None,
            "bid_exchange": numeric(row.get("bid_exchange")), "ask_exchange": numeric(row.get("ask_exchange")),
            "bid_condition": numeric(row.get("bid_condition")), "ask_condition": numeric(row.get("ask_condition")),
        }
        greek = greeks.get((strike, right))
        if greek and greek.get("underlying_timestamp") and quote["bid"] is not None and quote["ask"] is not None:
            greek_ts = timestamp(greek.get("timestamp"), theta_local=True)
            model_spot_ts = timestamp(greek["underlying_timestamp"], theta_local=True)
            model_spot = numeric(greek.get("underlying_price"))
            same_bbo = (quote["bid"], quote["ask"]) == (numeric(greek.get("bid")), numeric(greek.get("ask")))
            if (greek_ts == quote_ts and same_bbo and model_spot is not None and model_spot > 0
                    and datetime.fromisoformat(model_spot_ts) <= captured):
                quote.update(delta=numeric(greek.get("delta")), gamma=numeric(greek.get("gamma")),
                             iv=numeric(greek.get("implied_vol")), greeks_ts=greek_ts,
                             greeks_source="VENDOR", greeks_underlying_ts=model_spot_ts,
                             greeks_underlying_price=model_spot, iv_error=numeric(greek.get("iv_error")))
        quotes.append(quote)

    return {
        "schema_version": 1, "symbol": symbol, "source": "THETA", "source_kind": source_kind,
        "captured_at_utc": captured.isoformat(), "received_at_utc": received,
        "underlying": {"symbol": symbol, "price": spot_price, "quote_ts": spot_ts, "source": "THETA",
                       "price_kind": "INDEX_LAST" if symbol == "XSP" else "NASDAQ_BASIC_LAST_TRADE"},
        "quotes": quotes,
        "expiration_dates": [expiration],
        "source_scope": "Theta Terminal v3 current-day NBBO snapshot for one expiration and bounded strikes. "
                        "The operator declares the option quote entitlement."
                        + (" Same-quote Theta vendor Black-Scholes Greeks when available." if with_greeks else ""),
    }


def theta_same_day_time(value, captured, capture_date):
    normalized = timestamp(value, theta_local=True)
    event = datetime.fromisoformat(normalized)
    if event > captured or event.astimezone(NEW_YORK).date() != capture_date:
        raise CaptureError("ThetaData snapshot returned a future or non-current-day timestamp")
    return normalized


def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("status", help="Check default loopback TCP ports without making market requests")
    handoff = modes.add_parser("handoff", help="Validate normalized licensed NDJSON and write it to stdout")
    handoff.add_argument("file", help="NDJSON path, or - for stdin")
    theta = modes.add_parser("theta-snapshot", help="Request one bounded snapshot from an existing Theta Terminal v3")
    theta.add_argument("--symbol", choices=SYMBOLS, required=True)
    theta.add_argument("--expiration", type=iso_date, required=True)
    theta.add_argument("--strike-range", type=int, default=10, metavar="1..50")
    theta.add_argument("--source-kind", choices=("LIVE_BBO", "DELAYED_BBO"), required=True,
                       help="Classify the account's verified option snapshot entitlement")
    theta.add_argument("--with-greeks", action="store_true", help="Join same-quote Greeks from the Pro endpoint")
    theta.add_argument("--confirm-local-use", action="store_true",
                       help="Confirm your existing agreement permits automated retrieval and private local retention")
    args = parser.parse_args(argv)
    try:
        if args.mode == "status":
            print(json.dumps(status(), indent=2))
        elif args.mode == "handoff":
            if args.file == "-":
                sys.stdout.buffer.write(validated_handoff(sys.stdin.buffer))
            else:
                with open(args.file, "rb") as handle:
                    sys.stdout.buffer.write(validated_handoff(handle))
        else:
            if not args.confirm_local_use:
                raise CaptureError("ThetaData capture requires confirmed retrieval and local retention rights")
            if not 1 <= args.strike_range <= 50:
                raise CaptureError("Strike range must be between 1 and 50")
            packet = theta_snapshot(args.symbol, args.expiration, args.strike_range, args.source_kind, args.with_greeks)
            print(json.dumps(packet, allow_nan=False, separators=(",", ":")))
    except (CaptureError, OSError) as error:
        detail = str(error) if isinstance(error, CaptureError) else type(error).__name__
        print(f"Capture failed: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
