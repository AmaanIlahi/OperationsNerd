"""
Questionnaire answer validation.

Job: given a loaded Pack and a dict of submitted answers (keyed by question
id), check that every answer matches what that question declares -- right
type, and for multiselect, only options the pack actually allows.

This is deliberately separate from packs/loader.py's validate_pack(), even
though the pattern looks similar. That function checks a pack's internal
consistency, once, when the pack is loaded. This function checks a
business owner's *answers* against an already-valid pack, every time someone
submits the questionnaire. Different inputs, different lifetimes, same
philosophy: fail loudly, name the exact problem, never guess.
"""

from packs.loader import Pack, Question


def validate_answers(pack: Pack, answers: dict) -> list[str]:
    """Returns a list of human-readable issues. Empty list means the answers
    are clean and ready to be turned into settings_json."""
    issues = []

    for question in pack.questions:
        if question.id not in answers:
            issues.append(f"Missing answer for required question: {question.id}")
            continue

        value = answers[question.id]
        issues.extend(_validate_one(question, value))

    return issues


def _validate_one(question: Question, value) -> list[str]:
    issues = []

    if question.type == "text":
        if not isinstance(value, str):
            issues.append(f"{question.id}: expected text, got {type(value).__name__}")

    elif question.type == "number":
        # bool is technically a subclass of int in Python, exclude it explicitly
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(f"{question.id}: expected a number, got {type(value).__name__}")

    elif question.type == "boolean":
        if not isinstance(value, bool):
            issues.append(f"{question.id}: expected true/false, got {type(value).__name__}")

    elif question.type == "multiselect":
        if not isinstance(value, list):
            issues.append(f"{question.id}: expected a list of options, got {type(value).__name__}")
        else:
            allowed = set(question.options or [])
            invalid = [v for v in value if v not in allowed]
            if invalid:
                issues.append(
                    f"{question.id}: invalid option(s) {invalid}, "
                    f"allowed values are {sorted(allowed)}"
                )

    else:
        issues.append(f"{question.id}: pack declares unknown question type '{question.type}'")

    return issues


def answers_to_settings(pack: Pack, answers: dict) -> dict:
    """Converts {question_id: value} into {settings_key: value} for storage
    in businesses.settings_json. Only call this after validate_answers()
    returns no issues -- this function assumes the answers are already clean."""
    by_id = {q.id: q.settings_key for q in pack.questions}
    return {by_id[qid]: value for qid, value in answers.items() if qid in by_id}
