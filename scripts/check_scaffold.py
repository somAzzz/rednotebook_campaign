"""Verify the scaffold and document links without models or network access."""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cli(command):
    completed = subprocess.run(
        [sys.executable, "-m", "rednotebook", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def main():
    doctor = cli("doctor")
    assert doctor["python_ok"] and doctor["sqlite_ok"], doctor
    status = cli("status")
    assert status["stage"] == "S8-playwright-mcp"
    assert {"status", "doctor", "import", "quality", "metrics rank"} <= set(status["implemented"])
    assert "analyse" in status["implemented"]
    brief = json.loads((ROOT / "examples/brief.cando.synthetic.json").read_text())
    assert brief["synthetic"] is True
    assert all(fact["status"] == "unverified" for fact in brief["facts"])
    policy = tomllib.loads((ROOT / "config/policy.example.toml").read_text())
    assert not policy["execution"]["online_sources_enabled"]
    checked = 0
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            local = target.split("#", 1)[0]
            assert (path.parent / local).exists(), f"{path}: broken link {target}"
            checked += 1
    print(
        json.dumps(
            {
                "result": "pass",
                "local_links_checked": checked,
                "doctor": doctor,
                "scope": "scaffold only",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
