"""Explicit live Qwen smoke test. Synthetic only; never included in pytest."""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from rednotebook.importing import import_file
from rednotebook.research.config import ResearchBudget, load_config
from rednotebook.research.fixtures import research_fixture
from rednotebook.research.probe import probe
from rednotebook.research.render import markdown
from rednotebook.research.runner import analyse
from rednotebook.storage import Database
from rednotebook.util import now_utc


async def verify(config, out):
    result = await probe(config)
    summary = {
        "at": now_utc().isoformat(),
        "probe": result,
        "synthetic_only": True,
        "real_semantic_acceptance": False,
    }
    if result["structured_output"] and result["tool_calling"]:
        with TemporaryDirectory(prefix="rednotebook-s4-") as temporary:
            path = Path(temporary)
            grant, brief, rows = research_fixture(now_utc())
            (path / "evidence.json").write_text(json.dumps(rows, ensure_ascii=False))
            with Database(path / "test.sqlite") as db:
                imported = import_file(db, path / "evidence.json", grant)
                assert imported["state"] == "complete"
                report = await analyse(
                    db,
                    brief,
                    config,
                    ResearchBudget(max_notes=3, max_comments=6, batch_notes=3, max_model_calls=8),
                )
                summary["research"] = {
                    k: report[k]
                    for k in (
                        "state",
                        "selected_notes",
                        "selected_comments",
                        "completed_batches",
                        "errors",
                        "usage",
                        "tool_calls",
                    )
                }
                summary["research"]["findings"] = len(report["findings"])
                summary["research"]["matched_citations"] = sum(
                    len(f["support"]) + len(f["counter"]) for f in report["findings"]
                )
                out.mkdir(parents=True, exist_ok=True)
                (out / "research.md").write_text(markdown(report))
                (out / "research.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    out.mkdir(parents=True, exist_ok=True)
    (out / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return (
        0
        if summary.get("research", {}).get("state") == "complete"
        and summary["research"]["findings"] > 0
        else 2
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("private/model.toml"))
    parser.add_argument("--out", type=Path, default=Path("artifacts/s4-live"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(verify(load_config(args.config), args.out)))
