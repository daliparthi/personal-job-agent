import pytest

from app.scoring import cosine, extract_keywords, score, title_alignment

RESUME = "Data engineer. Python, SQL, Airflow and AWS. Built ETL pipelines that load sales data nightly."
JD = ("We are hiring a Data Engineer. You will build ETL pipelines with Python, SQL and Spark on AWS. "
      "Spark experience is required. Kubernetes is a plus.")


def test_score_shape_and_bounds():
    sc = score(RESUME, JD, "Data Engineer")
    assert {"score", "base", "coverage", "similarity", "title_alignment", "matched", "missing", "aliases", "where",
            "adjustments", "experience", "seniority", "knockouts", "evidence", "requirements"} <= set(sc)
    assert sc["adjustments"] == [] and sc["score"] == sc["base"]  # no profile: no adjustments
    assert 0 <= sc["score"] <= 100
    assert {"Python", "SQL", "AWS", "ETL"} <= set(sc["matched"])
    missing = {m["keyword"]: m for m in sc["missing"]}
    assert {"Apache Spark", "Kubernetes"} <= set(missing)
    assert missing["Apache Spark"]["count"] == 2
    assert "Spark" in missing["Apache Spark"]["context"]
    assert "Spark" in sc["aliases"]["Apache Spark"]


def test_adding_missing_skills_raises_the_score():
    before = score(RESUME, JD, "Data Engineer")["score"]
    after = score(RESUME + " Apache Spark and Kubernetes.", JD, "Data Engineer")["score"]
    assert after > before


def test_missing_sorted_by_weight():
    sc = score("nothing relevant", JD, "Data Engineer")
    weights = [m["weight"] for m in sc["missing"]]
    assert weights == sorted(weights, reverse=True)


def test_employer_name_is_not_a_skill():
    jd = "Join Snowflake to build the Data Cloud with Python."
    assert "Snowflake" not in extract_keywords(jd, company="Snowflake Inc.")
    assert "Snowflake" in extract_keywords(jd, company="Acme")


def test_repeated_acronyms_and_extra_keywords_count():
    jd = "Experience with ZQX tooling. ZQX certification preferred. Must know Frobnicator."
    kws = extract_keywords(jd, extra=["Frobnicator", "Python"])
    assert kws["ZQX"]["count"] == 2
    assert kws["Frobnicator"]["weight"] == 2.0
    assert "Python" not in kws  # not in the JD: an extra keyword only counts when the posting mentions it


def test_title_keyword_weighs_more():
    jd = "Python. Java."
    kws = extract_keywords(jd, title="Python Developer")
    assert kws["Python"]["weight"] > kws["Java"]["weight"]


def test_empty_inputs():
    sc = score("", "", "")
    assert sc["score"] == 0 and sc["matched"] == [] and sc["missing"] == []


def test_cosine():
    assert cosine("python sql spark", "python sql spark") == pytest.approx(1.0)
    assert cosine("", "anything") == 0.0
    assert 0 < cosine("python sql", "python java") < 1


def test_title_alignment_ignores_seniority_words():
    assert title_alignment("python developer", "Senior Python Developer") == 1.0
    assert title_alignment("python", "Senior Python Developer II") == 0.5
    assert title_alignment("anything", "Senior") == 0.0
