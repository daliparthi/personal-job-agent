import pytest

from app.jobparse import (classify_employment, classify_remote, contains_all, html_to_text, is_us_location,
                          keywords_present, looks_non_us, parse_salary, posted_days_ago, split_keywords, states_in)


# ---------------------------------------------------------------- salary
@pytest.mark.parametrize("text, lo, hi", [
    ("Pay range: $120,000 - $150,000 per year", 120_000, 150_000),
    ("Base salary $120K-$150K", 120_000, 150_000),
    ("200,000 USD - 322,000 USD annually", 200_000, 322_000),
    ("$136,900.00-193,270.00 USD", 136_900, 193_270),
    ("The range is $150,000 to $120,000.", 120_000, 150_000),  # reversed order is fixed up
    ("Hourly: $40 - $60 per hour", 40 * 2080, 60 * 2080),       # hourly -> annual (2080 h)
])
def test_parse_salary_ranges(text, lo, hi):
    smin, smax, snippet = parse_salary(text)
    assert (smin, smax) == (lo, hi)
    assert snippet and len(snippet) <= 80


def test_parse_salary_takes_widest_of_several_ranges():
    text = "Colorado: $100,000 - $130,000.\nNew York: $115,000 - $160,000."
    assert parse_salary(text)[:2] == (100_000, 160_000)


@pytest.mark.parametrize("text", [
    "3 - 5 years of experience",          # no currency marker
    "Team of 10 - 20 engineers",
    "$5 - $6 million in revenue",          # out of range for a salary
    "",
    None,
])
def test_parse_salary_ignores_non_salaries(text):
    assert parse_salary(text) == (None, None, "")


# ---------------------------------------------------------------- employment type
@pytest.mark.parametrize("wst, time_type, title, body, want", [
    ("Contract", "", "", "", "Contract"),
    ("Contingent Worker", "", "", "", "Contract"),
    ("Intern", "", "", "", "Internship"),
    ("Fixed Term", "", "", "", "Temporary"),
    ("Regular", "Part time", "", "", "Part-time"),
    ("Regular", "Full time", "", "", "Full-time"),
    ("", "", "Software Engineer Intern", "", "Internship"),
    ("", "", "Data Engineer (Contract)", "", "Contract"),
    ("", "", "Seasonal Warehouse Associate", "", "Temporary"),
    ("", "", "Engineer", "This is a 6-month contract role.", "Contract"),
    ("", "", "Engineer", "This is a temporary position.", "Temporary"),
    ("", "", "Engineer", "", "Full-time"),
    ("", "Part time", "Engineer", "", "Part-time"),
])
def test_classify_employment(wst, time_type, title, body, want):
    assert classify_employment(wst, time_type, title, body) == want


# ---------------------------------------------------------------- remote / hybrid
@pytest.mark.parametrize("remote_type, locations, title, body, want", [
    ("Hybrid", [], "", "", "Hybrid"),
    ("Flexible", [], "", "", "Hybrid"),
    ("Remote", [], "", "", "Remote"),
    ("Work from home", [], "", "", "Remote"),
    ("", ["Remote - US"], "", "", "Remote"),
    ("", [], "Data Engineer (Remote)", "", "Remote"),
    ("On-site", [], "", "", "On-site"),
    ("", ["Austin, TX"], "", "This role is fully remote.", "Remote"),
    ("", [], "", "We offer a hybrid schedule.", "Hybrid"),
    ("", [], "", "This is an on-site role.", "On-site"),
    ("", [], "", "", "Unspecified"),
])
def test_classify_remote(remote_type, locations, title, body, want):
    assert classify_remote(remote_type, locations, title, body) == want


# ---------------------------------------------------------------- locations
@pytest.mark.parametrize("loc, want", [
    ("Austin, TX", {"TX"}),
    ("Seattle, Washington", {"WA"}),
    ("Charleston, West Virginia", {"WV"}),
    ("Richmond, Virginia", {"VA"}),
    ("Washington, DC", {"DC"}),
    ("US-CA-Santa Clara", {"CA"}),
    ("Indianapolis, IN", {"IN"}),
    ("San Jose", {"CA"}),
    ("Bangalore, India", set()),
    ("", set()),
    (None, set()),
])
def test_states_in(loc, want):
    assert states_in(loc) == want


@pytest.mark.parametrize("text, want", [
    ("India, Bangalore", True),
    ("Canada - Toronto", True),
    ("Austin, TX", False),
    ("", False),
])
def test_looks_non_us(text, want):
    assert looks_non_us(text) is want


@pytest.mark.parametrize("loc, want", [
    ("United States", True), ("USA", True), ("Austin, TX", True), ("US, Remote", True),
    ("Toronto, Canada", False), ("", False),
])
def test_is_us_location(loc, want):
    assert is_us_location(loc) is want


# ---------------------------------------------------------------- dates, html, keywords
@pytest.mark.parametrize("text, want", [
    ("Posted Today", 0), ("Posted 5 hours ago", 0), ("Posted Yesterday", 1),
    ("Posted 3 Days Ago", 3), ("Posted 30+ Days Ago", 31), ("", None), (None, None),
])
def test_posted_days_ago(text, want):
    assert posted_days_ago(text) == want


def test_html_to_text_keeps_structure():
    html = "<h2>About</h2><p>We build&nbsp;things.</p><ul><li>Python</li><li>SQL</li></ul><p></p><p>End</p>"
    assert html_to_text(html) == "About\n\nWe build things.\n\n• Python\n\n• SQL\n\nEnd"
    assert html_to_text(None) == ""


def test_split_keywords():
    assert split_keywords("python, SQL;\n data engineer ,,") == ["python", "SQL", "data engineer"]
    assert split_keywords(None) == []


def test_keyword_matching_is_whole_word_and_case_insensitive():
    text = "Senior Python developer with PostgreSQL and C++"
    assert contains_all(text, ["python", "postgresql"])
    assert not contains_all(text, ["python", "java"])
    assert not contains_all(text, ["SQL"])  # "PostgreSQL" is not "SQL"
    assert keywords_present(text, ["c++", "go", "PYTHON"]) == ["c++", "PYTHON"]
