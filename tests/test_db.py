"""Schema creation and the merge behaviour of the observation upserts."""

from pipeline import db


def test_schema_creates_all_tables(tmp_path):
    conn = db.connect(tmp_path / "test.sqlite")
    tables = {row["name"] for row in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"bonds", "observations", "trade_summary", "fills", "files"} <= tables


def test_quote_and_volume_merge_into_one_row(tmp_path):
    """Quotes and volume arrive in different files; both upserts must land on
    the same (obs_date, isin, 'pdmo_daily') row without clobbering each other."""
    conn = db.connect(tmp_path / "test.sqlite")
    db.upsert_volume(conn, "2026-08-28", "LKB00934F154", 10_903_000_000)
    db.upsert_quote(conn, "2026-08-28", "LKB00934F154",
                    bid_yield=11.5, offer_yield=11.3,
                    bid_price=101.2, offer_price=101.5, raw_ref="abc")
    rows = conn.execute("SELECT * FROM observations").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["volume_lkr"] == 10_903_000_000
    assert row["bid_yield"] == 11.5
    assert row["mid_yield"] == 11.4
    assert row["executable"] == 0

    # Re-parsing the same file must be idempotent, not duplicate.
    db.upsert_quote(conn, "2026-08-28", "LKB00934F154",
                    bid_yield=11.5, offer_yield=11.3,
                    bid_price=101.2, offer_price=101.5, raw_ref="abc")
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 1


def test_bond_upsert_keeps_known_values(tmp_path):
    conn = db.connect(tmp_path / "test.sqlite")
    db.upsert_bond(conn, "LKB00934F154", 9.0, "2034-06-15", 9, "2026-04-15")
    # A later sighting without a coupon must not erase the known coupon,
    # and first_seen_date must only ever move earlier.
    db.upsert_bond(conn, "LKB00934F154", None, "2034-06-15", 9, "2026-01-05")
    row = conn.execute("SELECT * FROM bonds").fetchone()
    assert row["coupon_pct"] == 9.0
    assert row["first_seen_date"] == "2026-01-05"


def test_record_file_partial_updates(tmp_path):
    conn = db.connect(tmp_path / "test.sqlite")
    db.record_file(conn, "http://x/1", sha256="aa", file_type="volumes")
    db.record_file(conn, "http://x/1", parse_status="ok", parse_note="12 rows")
    row = conn.execute("SELECT * FROM files").fetchone()
    assert row["sha256"] == "aa"           # untouched by the second call
    assert row["parse_status"] == "ok"


def test_lkr_from_millions_is_exact():
    assert db.lkr_from_millions(232.825) == 232_825_000
    assert db.lkr_from_millions(6241.28) == 6_241_280_000
