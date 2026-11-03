"""Clause 13: the README section names the commands, the New York window, the billing and no code."""

from pathlib import Path

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_skunkworks_section():
    sec = README.split("## Skunkworks", 1)[1].split("\n## ", 1)[0]
    low = sec.lower()
    for verb in ("run", "status", "show", "stop", "decide"):
        assert f"`{verb}`" in sec
    assert "09:30-16:00 new york time" in low
    assert "claude quota" in low and "mesh queue" in low
    assert "approval produces no code" in low
