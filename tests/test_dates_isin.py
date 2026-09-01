"""Edge cases for date parsing and the ISIN codec — every case below was
observed in a real file or index page during sample inspection."""

from datetime import date

from pipeline import dates, isin


# ---------------------------------------------------------------------------
# Excel serials
# ---------------------------------------------------------------------------

def test_excel_serial_roundtrip():
    assert dates.from_excel_serial(46265) == date(2026, 8, 31)
    assert dates.from_excel_serial(46645.0) == date(2027, 9, 15)


def test_excel_serial_rejects_garbage():
    # A real corrupted maturity cell held 14472 (= year 1939).
    assert dates.from_excel_serial(14472) is None
    assert dates.from_excel_serial("not a number") is None
    assert dates.from_excel_serial(None) is None


# ---------------------------------------------------------------------------
# Index-page labels (day-first, with observed whitespace damage)
# ---------------------------------------------------------------------------

def test_parse_dmy_clean():
    assert dates.parse_dmy("as at 19.12.2025") == date(2025, 12, 19)


def test_parse_dmy_with_stray_spaces():
    assert dates.parse_dmy("> 16 .04.2026") == date(2026, 4, 16)
    assert dates.parse_dmy("as at 26.01.202 6") == date(2026, 1, 26)


def test_parse_dmy_absent():
    assert dates.parse_dmy("Daily Summary Report") is None


# ---------------------------------------------------------------------------
# Free-text titles
# ---------------------------------------------------------------------------

def test_parse_spelled_month():
    assert dates.parse_spelled("volumes on 28 August 2026") == date(2026, 8, 28)
    assert dates.parse_spelled("HELD ON 25 th August 2026") == date(2026, 8, 25)


def test_ambiguous_numeric_both_readings():
    dmy, mdy = dates.parse_ambiguous_numeric("on 04.10.2026")
    assert (dmy, mdy) == (date(2026, 10, 4), date(2026, 4, 10))


def test_ambiguous_numeric_one_impossible():
    # "12.18.2025" can only be month-first (there is no month 18).
    dmy, mdy = dates.parse_ambiguous_numeric("on 12.18.2025")
    assert dmy is None
    assert mdy == date(2025, 12, 18)


# ---------------------------------------------------------------------------
# ISIN codec (samples verified against real volumes files)
# ---------------------------------------------------------------------------

def test_decode_known_isins():
    assert isin.decode("LKB00934F154") == (9, date(2034, 6, 15))
    assert isin.decode("LKB02039H156") == (20, date(2039, 8, 15))
    assert isin.decode("LKB00527I150") == (5, date(2027, 9, 15))


def test_build_reproduces_real_isins():
    assert isin.build(9, date(2034, 6, 15)) == "LKB00934F154"
    assert isin.build(11, date(2036, 8, 15)) == "LKB01136H151"
    assert isin.build(4, date(2026, 5, 15)) == "LKB00426E154"


def test_decode_rejects_bad_check_digit():
    assert isin.decode("LKB00934F155") is None  # last digit off by one


def test_decode_rejects_bills_and_noise():
    assert isin.decode("LKA09126J300") is None  # a T-bill, not a bond
    assert isin.decode("Total") is None


# ---------------------------------------------------------------------------
# Series labels — the key that joins a quote to an ISIN, so the two sources
# must normalise identically (quote sheet vs auction press release).
# ---------------------------------------------------------------------------

def test_series_normalise_across_sources():
    from pipeline import series
    # Same bond as printed by the quote sheet and by an auction release.
    assert series.normalise("10.00%2030A") == series.normalise("10.00%2030 ‘A’")
    # Zero padding differs between releases.
    assert series.normalise("09.50%2030 ‘A’") == "9.50%2030A"
    assert series.normalise("18.00%2031A ") == "18.00%2031A"


def test_series_keeps_every_step_coupon():
    from pipeline import series
    # Restructuring bonds chain rates; dropping later steps would merge
    # two genuinely different bonds under one key.
    assert series.normalise("12.4%7.5%5%2029A") == "12.40%7.50%5.00%2029A"
    assert series.coupon_steps("12%9%2027A") == [12.0, 9.0]
    assert series.coupon_steps("11.25%2026A") == [11.25]


def test_series_rejects_non_labels():
    from pipeline import series
    assert series.normalise("Total") is None
    assert series.normalise("") is None
    assert series.normalise(None) is None
