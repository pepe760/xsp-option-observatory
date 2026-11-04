# XSP Option Observatory

A GitHub Pages dashboard for observing short put spread quotes across XSP,
SPY, QQQ, TLT and SOXX. Open a local observation export to browse private
strike and credit data. The public build contains the configured research
grid and collector status.

### Research grid

| Underlying | Exact wing widths in strike points |
| --- | --- |
| XSP | 15, 20, 40, 50 |
| SPY, QQQ | 5, 10, 15, 20, 40, 50 |
| TLT | 1, 2, 5, 10 |
| SOXX | 5, 10, 15, 20 |

The short put magnitude targets are 0.03, 0.05, 0.07, 0.08, 0.10, 0.12,
0.15, 0.20, 0.25, 0.30 and 0.40. Each target uses a band of min(0.02, half the target). The closest
eligible OTM standard put is selected first. Ties select the lower strike.
An exact wing must exist in the same root, expiry and trading class. A
missing wing or an unavailable delta remains a coverage gap.

The next scheduled NYSE session is the target expiry. Friday to Monday is
one session and three calendar days. The stored calendar covers 2026–2028
and limits selection to scheduled regular hours, including early closes.
Exceptional closures require a separate current exchange check. Each
symbol's actual listed expiries control availability. The collector can
archive quotes for other expiries and calls for later research.

A qualifying observation has synchronized underlying, two-leg BBO and
short-put Greeks with their model spot provenance. Clocks must be at or
before capture, within five seconds of it, and within two seconds of each
other. The Greek model spot must be within 0.1% of the observed spot.
Displayed size of at least two is required at short bid and long ask.
Natural credit is short bid minus long ask in strike points. Gross terminal
loss per standard spread is (width minus credit) × 100 before fees.
The same-snapshot quote is an observation. A forward fill would require a
separate later quote on the frozen pair and an independently reviewed book.

OHLC, delayed, indicative and historical BBO snapshots retain separate
source labels. OHLC cannot supply simultaneous bid/ask or displayed size.
Estimated Greeks are labeled by origin. The dashboard tracks quote
availability across settings. Strategy rules for holding, SL8, SMA200, event
filters, account performance and settlement belong to separate research.

### Private capture and storage

Python 3.10+ uses standard-library SQLite. Archive files and private exports
must be outside this checkout. Keep the database and any backups in an
owner-controlled location that your provider agreement permits.

```sh
python3 capture_feed.py status

# Import authorized, source-labeled NDJSON.
python3 capture_feed.py handoff /private/path/snapshots.ndjson |
  python3 observe.py ingest --db /private/path/options.sqlite3 --confirm-storage-rights

# Export recent observations. Choose this JSON file in the browser.
python3 observe.py export-private --db /private/path/options.sqlite3 \
  --output /private/path/observations.json
```

Enable the shell's `pipefail` or validate the exporter exit status in scheduled
jobs. The ingest transaction retains each normalized snapshot as gzip plus
SHA-256, alongside derived observations. The key is source, source kind,
symbol and UTC capture time. Byte-identical re-imports are idempotent.
Conflicting repeats fail without changing the original. A selection-code
or config change uses a new archive. The ingest receipt has a separate
import clock. Delayed and historical packets retain their original clocks.

Input schema v1 is one JSON object per line. The required top-level fields
are `schema_version`, `symbol`, `source`, `source_kind`, `captured_at_utc`,
`received_at_utc` and `quotes`. An optional `expiration_dates` list with
`expiry_inventory_complete=true` represents a verified complete expiry list.
`underlying` carries `symbol`, `price`, `quote_ts`, and `source`.
Each quote carries the same underlying `symbol`, ISO `expiry`, numeric
`strike`, `right` (`P` or `C`), `bid`, `ask`, `bid_size`, `ask_size`,
`quote_ts`, `delta`, `gamma`, `iv`, `greeks_ts`, `greeks_source`
(`VENDOR` or `MODEL`), `greeks_underlying_ts`,
`greeks_underlying_price`, `trading_class`, `multiplier` and
`standard_contract`. Unknown provider values may be null. All timestamps
use explicit timezone offsets. OHLC payloads use `source_kind=OHLC` with
`quotes=[]` and optional source `bars`.

The optional `capture_feed.py theta-snapshot` path reads one bounded expiry
from a working local Theta Terminal v3. `--confirm-local-use` requires
confirmed market-data entitlement and retrieval/storage rights. Use
`--help` for the flags. The current adapter preserves unknown standard
contract metadata as null. Qualifying that feed needs provider reference
metadata and synchronized underlying clocks. When a qualified exporter is
available, have it emit new packets on the desired schedule, for example
09:45, 10:00 and 15:55 Eastern, with actual capture timestamps. This
repository's Pages workflow runs validation and publication.

Use `archive_catalog.py MANIFEST --archive-dir DIR` to check locally
available XSP OHLC shards against their declared byte counts and SHA-256
hashes, then inspect a bounded header/time sample. Original source archives
can stay in their existing private storage.

### Yahoo Finance reference

The symbol picker links to Yahoo Finance's own option chain for each root,
including `^XSP` for the XSP index. The options pages label the data as delayed.
The displayed last trade date belongs to the last transaction. It does
not timestamp either side of the bid/ask. The chain does not display
bid/ask size or option delta.

Yahoo's [terms](https://legal.yahoo.com/tw/zh-hant/yahoo/terms/otos/index.html)
require prior permission for automated collection. If you have an
authorized Yahoo export you can retain, normalize it through the private
handoff above with `source=YAHOO_FINANCE` and `source_kind=INDICATIVE`.
Record the actual snapshot capture time separately, leave `quote_ts` and
other unknown fields null, and retain last trade time in a separate field
if supplied. Keep the export and derived private JSON out of Git. The
qualification engine classifies these packets as `INDICATIVE_ONLY`.

### GitHub Pages

Run `python3 build_site.py dist` and serve `dist` with a local HTTP server.
The Actions workflow publishes four explicit static files and validates
that public JSON exactly matches the configuration. The browser's private
file reader holds the file in memory for the open tab.

### 廣東話

網站可按標的、Delta 同翼寬睇資料覆蓋。開啟本機匯出 JSON 就睇到私人報價觀察。
日線或 15 分鐘 OHLC 另作歷史資料，收集到帶時間同數量嘅雙腿報價先可做同一時點比較。
