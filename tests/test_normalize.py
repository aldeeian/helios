from datetime import date

from src.reconciliation import normalize as nz


class TestNormalizeDepartment:
    def test_exact_aliases(self):
        assert nz.normalize_department("OPS") == "Operations"
        assert nz.normalize_department("Operations ") == "Operations"
        assert nz.normalize_department("People & Culture") == "Human Resources"
        assert nz.normalize_department("IT & Technology") == "Technology"

    def test_fuzzy_backstop(self):
        assert nz.normalize_department("Operatons") == "Operations"  # typo

    def test_unknown_returns_none(self):
        assert nz.normalize_department("Warehouse") is None
        assert nz.normalize_department("") is None
        assert nz.normalize_department(None) is None


class TestNormalizeFte:
    def test_ratio_passthrough(self):
        assert nz.normalize_fte(0.5) == 0.5
        assert nz.normalize_fte(1.0) == 1.0

    def test_percent_scale(self):
        assert nz.normalize_fte(50) == 0.5
        assert nz.normalize_fte("80") == 0.8

    def test_invalid_not_clamped(self):
        assert nz.normalize_fte(-0.2) is None
        assert nz.normalize_fte(250) is None
        assert nz.normalize_fte("abc") is None
        assert nz.normalize_fte(None) is None


class TestParseDateMulti:
    def test_formats(self):
        assert nz.parse_date_multi("2024-03-05") == date(2024, 3, 5)
        assert nz.parse_date_multi("05/03/2024") == date(2024, 3, 5)  # day-first
        assert nz.parse_date_multi("Mar 05, 2024") == date(2024, 3, 5)

    def test_bad_input(self):
        assert nz.parse_date_multi("") is None
        assert nz.parse_date_multi("not a date") is None
        assert nz.parse_date_multi(None) is None


class TestParseAmount:
    def test_currency_strings(self):
        assert nz.parse_amount("$12,345.67") == 12345.67
        assert nz.parse_amount("1234.5") == 1234.5
        assert nz.parse_amount(99) == 99.0

    def test_bad_input(self):
        assert nz.parse_amount("N/A") is None
        assert nz.parse_amount(None) is None


class TestNormalizePersonName:
    def test_whitespace_and_case(self):
        assert nz.normalize_person_name("  Maya  Chen  ") == "maya chen"
        assert nz.normalize_person_name("MAYA CHEN") == "maya chen"
