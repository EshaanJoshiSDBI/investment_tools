from mf_tracker.amcs.ppfas import _classify, _expiry, clean_text, number


def test_normalizers():
    assert clean_text("  A\xa0 B\n") == "A B"
    assert number("$0.00%", percent=True) == 0.0
    assert number("NIL") is None
    assert number("1,234.5") == 1234.5


def test_classification_and_expiry():
    assert _classify("Equity & Equity related", "Listed", "HDFC Bank") == ("domestic_equity", "equity")
    assert _classify("Derivatives", "Index", "NIFTY June 2026 Future", derivative=True) == ("index_future", "index_future")
    assert _classify("Derivatives", "Index / Stock Futures", "HDFC Bank June 2026 Future", derivative=True) == ("equity_future", "equity_future")
    assert _classify("Arbitrage", None, "HDFC Bank") == ("domestic_equity", "equity")
    assert _classify("Others", "Corporate Debt Market Development Fund", "Class A2 Units") == ("mutual_fund_unit", "mutual_fund_unit")
    assert _expiry("NIFTY June 2026 Future") == "2026-06"
