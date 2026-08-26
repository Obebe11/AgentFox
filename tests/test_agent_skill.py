import pathlib

def test_skill_md_exists():
    p = pathlib.Path(__file__).parent.parent / ".agent/skills/agentfox/SKILL.md"
    assert p.exists(), ".agent/skills/agentfox/SKILL.md missing"
    text = p.read_text(encoding="utf-8").lower()
    assert "goto" in text, "SKILL.md missing goto"
    assert "click" in text, "SKILL.md missing click"
    # ensure skill also has other commands for completeness
    assert "extract" in text, "SKILL.md missing extract"
    assert "snapshot" in text, "SKILL.md missing snapshot"
