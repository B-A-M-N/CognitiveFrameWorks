"""Keep WARD refusal and ANCHOR approach recovery as separate contracts.

These documents instruct agents rather than drive a deterministic runtime
state machine, so this test checks the distributed instruction contract. Live
agent behavior remains a separate qualification requirement.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_block_action_returns_to_fuse_and_only_recover_approach_triggers_anchor():
    ward = (ROOT / "WARD" / "SKILL.md").read_text(encoding="utf-8")
    ward_reference = (ROOT / "WARD" / "references" / "authority-boundaries.md").read_text(
        encoding="utf-8")
    ward_cheatsheet = (ROOT / "WARD" / "references" / "cheatsheet.md").read_text(
        encoding="utf-8")
    fuse = (ROOT / "FUSE" / "SKILL.md").read_text(encoding="utf-8")
    fuse_reference = (ROOT / "FUSE" / "references" / "execution-strategy.md").read_text(
        encoding="utf-8")
    anchor = (ROOT / "ANCHOR" / "SKILL.md").read_text(encoding="utf-8")

    assert "A refusal alone does not trigger recovery" in ward
    assert "A blocked action returns to FUSE for another candidate" in ward
    assert "same valid approach" in fuse
    assert "same valid approach" in fuse_reference
    assert "Only WARD's explicit `RECOVER_APPROACH`" in anchor
    assert "same valid approach" in ward_reference
    assert "same valid approach" in ward_cheatsheet

    obsolete_rules = (
        "| refuse | Not permitted | Block. Escalate to ANCHOR Recovery |",
        "Recovery Discipline trigger — approach must change",
        "| Action selected | `refuse` | Do not execute. Escalate to ANCHOR Recovery Discipline.",
    )
    for document in (ward_reference, ward_cheatsheet, fuse_reference):
        assert not any(rule in document for rule in obsolete_rules)
