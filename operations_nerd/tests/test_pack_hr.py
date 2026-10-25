"""HR pack: loads clean through the same two-layer validation every pack gets,
and carries the four HR feature lanes the pack promises (leave, onboarding,
recruiting, employee self-service) with approval defaults for each action."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from packs.loader import list_packs, load_pack


def main():
    pack = load_pack("hr")

    assert pack.id == "hr"
    assert pack.display_name == "HR / People Ops"

    actions = {at for s in pack.event_schemas for at in s.valid_action_types}
    expected = {
        "send_leave_decision_reply",   # leave & PTO management
        "send_onboarding_pack",        # onboarding checklist
        "send_application_reply",      # recruiting / ATS acknowledgement
        "schedule_interview",          # recruiting / ATS interview
        "send_hr_info_reply",          # employee self-service answers
        "escalate_to_hr_manager",      # human handoff
    }
    assert expected <= actions, f"missing actions: {expected - actions}"
    for action in expected:
        assert action in pack.approval_defaults, f"no approval default for {action}"
        assert f"templates['{action}']" or action in pack.prompts, f"no prompt for {action}"

    entity_types = [e.entity_type for e in pack.entity_schemas]
    assert entity_types == ["crm_employee", "crm_candidate"], entity_types

    listed = {p["id"] for p in list_packs()}
    assert "hr" in listed, "hr pack not discoverable via list_packs()"

    print("PASS: hr pack loads clean, all four HR lanes wired, discoverable")


if __name__ == "__main__":
    main()
