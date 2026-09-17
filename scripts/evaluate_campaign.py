"""Validate synthetic cases, optionally run explicitly selected cases on a local model.

Results do not judge themselves: author-review criteria remain pending even if all
program checks pass. A separate output directory/DB avoids real research data.
"""

import argparse
import asyncio
import json
from pathlib import Path

from rednotebook import campaign
from rednotebook.campaign_contracts import ContentBrief
from rednotebook.research.config import load_config
from rednotebook.storage import Database
from rednotebook.util import atomic_json, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases", type=Path, default=Path("examples/campaign-evaluation.synthetic.json")
    )
    parser.add_argument("--run-model", action="store_true")
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--config", type=Path, default=Path("private/model.toml"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        assert ContentBrief.model_validate(case["brief"]).synthetic
        assert case["author_review_criteria"]
    if not args.run_model:
        print(json.dumps({"validated_synthetic_cases": len(cases), "model_called": False}))
        return
    if not args.out or not args.ids or not set(args.ids) <= {c["id"] for c in cases}:
        parser.error("--run-model requires --out and valid explicit --ids")
    args.out.mkdir(parents=True, exist_ok=False)
    config = load_config(args.config)

    async def run():
        with Database(args.out / "synthetic.sqlite") as db:
            for case in cases:
                if case["id"] not in args.ids:
                    continue
                row = campaign.create(db, ContentBrief.model_validate(case["brief"]))
                result = await campaign.generate(db, row["id"], row["version"], config)
                report = {
                    "case_id": case["id"],
                    "case_hash": digest(case),
                    "author_review_criteria": case["author_review_criteria"],
                    "author_evaluation": "pending",
                    "synthetic": True,
                    "result": result,
                }
                atomic_json(args.out / f"{case['id']}.json", report)
                print(
                    json.dumps(
                        {
                            "case": case["id"],
                            "state": result.get("state"),
                            "stage": result.get("payload", {}).get("generation", {}).get("stage"),
                            "error_code": result.get("error_code"),
                            "author_evaluation": "pending",
                        }
                    ),
                    flush=True,
                )

    asyncio.run(run())


if __name__ == "__main__":
    main()
