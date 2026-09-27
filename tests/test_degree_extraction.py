"""A summer school is not a degree.

Anish Chandra, ticket #36:

    It analysed my resume and 8 mentioned MBA but I haven't done everyday I
    have just attended summer program that is called masters union MBA summer
    program so yes can you fix that part I am BCom graduate from RA POADR
    COLLEGE OF COMMERCE AND ECONOMICS

His resume says, on two separate lines:

    MASTER'S UNION MBA Summer School                     Mumbai | 2026
    Bachelor of Commerce | Majors in Business Management | CGPA: 7.01/10

The old extractor scanned the whole document for degree tokens with no regard
for the line they sat on, so "MBA" inside the name of a summer school became a
degree he holds, while "Bachelor of Commerce" spelled out matched nothing at
all. Both halves of that are wrong, and the second is why nobody noticed: the
screen showed one confident wrong answer instead of a missing one.

This is not only a display bug. `_extract_candidate_profile` feeds education
into the outreach email generator, so a fabricated degree gets written into
mail sent in the student's own name, to real recruiters.

The tests below assert on the extracted CONTENT, not just that something came
back, and include the exact line from his resume.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.candidate_intelligence.parser import _extract_degrees


ANISH = """ANISH CHANDRA
canish8971@gmail.com  |  +91 9137371642
EDUCATION
MASTER'S UNION MBA Summer School                          Mumbai | 2026
Business, Strategy and Finance coursework
R. A. PODAR COLLEGE OF COMMERCE AND ECONOMICS             Mumbai | 2025
Bachelor of Commerce | Majors in Business Management | CGPA: 7.01/10
THE DADAR PARSEE YOUTHS ASSEMBLY HIGH SCHOOL              Mumbai | 2020
"""


def test_the_reported_bug_a_summer_school_is_not_a_degree():
    """The ticket, exactly: he must read as BCom and never as MBA."""
    degrees = _extract_degrees(ANISH)
    assert "MBA" not in degrees, f"summer school reported as a degree: {degrees}"
    assert "BCom" in degrees, f"his actual degree is missing: {degrees}"


def test_a_real_mba_is_still_found():
    """The fix must not cost us the true positives it was guarding."""
    resume = """EDUCATION
INDIAN INSTITUTE OF MANAGEMENT AHMEDABAD                  2024
MBA | Marketing and Strategy
"""
    assert "MBA" in _extract_degrees(resume)


def test_spelled_out_degrees_are_found():
    """"Bachelor of Commerce" was invisible to the token scan."""
    assert "BCom" in _extract_degrees("Bachelor of Commerce, Podar College, 2025")
    assert "BTech" in _extract_degrees("Bachelor of Technology in Computer Science")
    assert "MBA" in _extract_degrees("Master of Business Administration, ISB")
    assert "PhD" in _extract_degrees("Doctor of Philosophy, Physics")


def test_short_programmes_never_count_as_degrees():
    """Every shape a non-degree arrives in, not just the one from the ticket."""
    for line in [
        "MBA Summer School, Master's Union",
        "MBA Bootcamp 2025",
        "Certificate in Business Analytics (MBA level)",
        "MBA Aspirants Society, college chapter",
        "Attended an MBA masterclass",
        "MBA preparation coaching",
        "Marketing Intern, MBA cohort project",
        "Winter School in Data Science, MSc modules",
    ]:
        assert _extract_degrees(line) == [], f"counted as a degree: {line!r}"


def test_a_degree_is_still_found_when_a_programme_sits_next_to_it():
    """Disqualifying one line must not disqualify the whole resume."""
    resume = """EDUCATION
MBA Summer School, Master's Union                         2026
Bachelor of Commerce, R. A. Podar College                 2025
"""
    degrees = _extract_degrees(resume)
    assert degrees == ["BCom"], degrees


def test_the_old_scan_would_have_failed_this():
    """Prove the check fails on the actual bug.

    This is the pre-fix expression. If someone restores it, the assertions
    above are what stop it reaching a student's resume screen again.
    """
    import re
    old = re.compile(
        r'(?i)\b(B\.?Tech|B\.?E|B\.?Sc|B\.?Com|B\.?A|BBA|BCA|M\.?Tech|M\.?E|M\.?Sc'
        r'|M\.?Com|M\.?A|MBA|MCA|Ph\.?D|Diploma)\b'
    )
    old_result = []
    for m in old.findall(ANISH):
        if m not in old_result:
            old_result.append(m)

    assert "MBA" in old_result, "the old scan invented the MBA"
    assert "BCom" not in old_result, "the old scan also missed his real degree"
    assert _extract_degrees(ANISH) != old_result
