import pytest

from course_planner.schedule import (
    PlanningSession,
    PlanRequest,
    ScheduledSection,
    Section,
    Term,
)


def test_confirming_a_term_advances_a_new_session_and_retains_its_courses():
    fall, spring = Term(2026, "Fall"), Term(2027, "Spring")
    session = PlanningSession(PlanRequest(fall, spring, {}))
    section = ScheduledSection(Section(1, "END 101E", "Fall", ()), fall)

    confirmed = session.confirm([section])

    assert session.confirmed == ()
    assert confirmed.current_term == spring
    assert confirmed.confirmed_codes == {"END 101E"}
    assert confirmed.confirm([]).current_term == Term(2027, "Summer")
    with pytest.raises(ValueError, match="current planning term"):
        confirmed.confirm([section])
