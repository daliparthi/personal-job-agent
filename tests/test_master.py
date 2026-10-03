import copy
import os
import re
import time

import pytest

from app import config, db, master
from tests.conftest import SAMPLE_MASTER


@pytest.mark.parametrize("text, start, end, rest", [
    ("Data Engineer, Acme | Jan 2020 - Present", "Jan 2020", "Present", "Data Engineer, Acme"),
    ("Analyst (March 2016 – Dec 2018)", "March 2016", "Dec 2018", "Analyst"),
    ("Engineer | 2019 to 2021 | Remote", "2019", "2021", "Engineer | Remote"),
    ("Intern, Summer 2015 through Fall 2015", "Summer 2015", "Fall 2015", "Intern"),
    ("B.S. Computer Science, 2018", "", "2018", "B.S. Computer Science"),
    ("M.S. Data Science, Expected May 2027", "", "May 2027", "M.S. Data Science"),
    ("No dates here", "", "", "No dates here"),
])
def test_split_dates(text, start, end, rest):
    got_start, got_end, got_rest = master.split_dates(text)
    # tidy() can leave "a| b" where a piece was cut out; the fields are split on separators afterwards anyway.
    assert (got_start, got_end, re.sub(r"\s*\|\s*", " | ", got_rest)) == (start, end, rest)


def test_draft_contact():
    c = master.draft_contact(["Jane Doe", "Austin, TX | jane@example.com | 555-010-0100 | linkedin.com/in/jane"])
    assert c["name"] == "Jane Doe"
    assert c["contact"]["email"] == "jane@example.com"
    assert c["contact"]["phone"] == "555-010-0100"
    assert c["contact"]["location"] == "Austin, TX"
    assert c["contact"]["links"] == ["linkedin.com/in/jane"]


def test_draft_contact_headline_and_extras():
    c = master.draft_contact(["Jane Doe", "Senior Product Manager", "jane@example.com | jane@work.com"])
    assert c["headline"] == "Senior Product Manager"
    assert c["contact"]["other"] == ["jane@work.com"]


@pytest.mark.parametrize("kind, rest, want", [
    ("experience", "Senior Data Engineer, Acme Corp, Austin, TX",
     {"title": "Senior Data Engineer", "company": "Acme Corp", "location": "Austin, TX"}),
    ("experience", "Marketing Analyst at Globex", {"title": "Marketing Analyst", "company": "Globex", "location": ""}),
    ("experience", "Initech Inc | Product Designer | Remote",
     {"title": "Product Designer", "company": "Initech Inc", "location": "Remote"}),
    ("education", "Master of Science in Computer Science, University of Texas",
     {"degree": "Master of Science in Computer Science", "school": "University of Texas", "location": ""}),
    ("projects", "Open-source CLI", {"name": "Open-source CLI", "location": ""}),
])
def test_guess_fields(kind, rest, want):
    assert master.guess_fields(kind, rest) == want


def test_normalize_dump_parse_round_trip():
    data = copy.deepcopy(SAMPLE_MASTER)
    data["sections"][1]["groups"].append({"name": "Cloud", "items": "AWS (S3, EMR), Docker; Terraform"})
    data["sections"].append({"title": "", "kind": "weird", "items": ["Volunteer", None, " "]})
    norm = master.normalize(data)
    assert norm["sections"][1]["groups"][-1]["items"] == ["AWS (S3, EMR)", "Docker", "Terraform"]
    assert norm["sections"][-1] == {"title": "Other", "kind": "other", "items": ["Volunteer"], "style": "lines"}
    assert master.parse_yaml(master.dump(data, "note")) == norm


def test_entry_heading_kept_when_fields_would_drop_words():
    e = master._entry("experience", {"title": "Engineer", "company": "Acme", "_src": ["Engineer, Acme (via Globex)"]})
    assert e["heading"] == ["Engineer, Acme (via Globex)"]
    e = master._entry("experience", {"title": "Engineer", "company": "Acme", "_src": ["Engineer at Acme"]})
    assert e["heading"] == []


@pytest.mark.parametrize("bad", [None, [], "text", {"name": "", "sections": []}, {"name": "X", "sections": "nope"}])
def test_normalize_rejects_bad_data(bad):
    with pytest.raises(ValueError):
        master.normalize(bad)


def test_parse_yaml_reports_line():
    with pytest.raises(ValueError, match="line 2"):
        master.parse_yaml("name: X\n  bad: [\n")


def test_save_writes_file_and_db_and_new_upload_clears_tailored():
    db.save_tailored("j1", {"resume": {}}, 50, [], [])
    data, text = master.save(data=SAMPLE_MASTER, filename="cv.docx", new_upload=True, note="Built for a test")
    assert config.MASTER_YAML.read_text(encoding="utf-8") == text
    assert "# Built for a test" in text
    r = db.get_resume()
    assert r["filename"] == "cv.docx" and r["data"] == data
    assert "Northwind Analytics" in r["text"]
    assert db.get_tailored("j1") is None


def test_save_keeps_your_exact_yaml_text():
    yaml_text = "# my own comment\nname: Jordan Avery\nsections: []\n"
    master.save(yaml_text=yaml_text)
    assert config.MASTER_YAML.read_text(encoding="utf-8") == yaml_text


def test_sync_picks_up_outside_edits_and_survives_bad_yaml():
    master.save(data=SAMPLE_MASTER, filename="cv.docx")
    assert master.sync() == (False, None)
    _touch(config.MASTER_YAML, config.MASTER_YAML.read_text(encoding="utf-8").replace("Jordan Avery", "J. Avery"))
    assert master.sync() == (True, None)
    assert db.get_resume()["data"]["name"] == "J. Avery"
    _touch(config.MASTER_YAML, "name: [unclosed\n")
    changed, error = master.sync()
    assert not changed and "using the last good copy" in error
    assert db.get_resume()["data"]["name"] == "J. Avery"
    config.MASTER_YAML.unlink()
    assert master.sync() == (True, None)
    assert db.get_resume() is None
    assert master.read_yaml() == ""


def _touch(path, text):
    """Write and make sure the modification time moves (some file systems have coarse mtimes)."""
    before = path.stat().st_mtime if path.exists() else 0
    path.write_text(text, encoding="utf-8")
    if path.stat().st_mtime == before:
        os.utime(path, (time.time() + 2, before + 2))
