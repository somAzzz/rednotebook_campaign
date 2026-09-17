"""Read-only, batch-scoped gateway. Source data can never widen its permissions."""

import hashlib

from rednotebook.analysis.metrics import rank_observations
from rednotebook.errors import DomainError
from rednotebook.provenance import public_http_url
from rednotebook.research.contracts import Citation


class EvidenceGateway:
    ALLOWED_TOOLS = frozenset({"get_note_evidence", "get_comment_sample", "compare_metrics"})

    def __init__(self, db, observations, max_chars=1800, metric_context=None):
        self.db = db
        self.observations = observations
        self.by_id = {o["id"]: o for o in observations}
        self.catalog = {}
        self.truncated_fields = 0
        self.tool_calls = []
        self.metric_context = metric_context
        for index, observation in enumerate(observations):
            for field in ("title", "text", "media_text"):
                original = observation["content"].get(field, "")
                if not original:
                    continue
                self.truncated_fields += len(original) > max_chars
                bounded = original[:max_chars]
                # Keep all available text within the explicit field budget, but
                # give the model narrow deterministic spans to cite and review.
                start = 0
                while start < len(bounded):
                    end = min(start + 1200, len(bounded))
                    if end < len(bounded):
                        boundary = bounded.rfind("\n", start + 400, end)
                        if boundary != -1:
                            end = boundary + 1
                    excerpt = bounded[start:end]
                    ref = f"r{index}-{field}" + (f"-{start}" if start else "")
                    self.catalog[ref] = Citation(
                        evidence_id=observation["id"],
                        revision=observation["revision"],
                        source_id=observation["source_id"],
                        field=field,
                        start=start,
                        end=end,
                        excerpt=excerpt,
                        sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
                    )
                    start = end
        self.max_chars = max_chars

    def check_permissions(self):
        for source in {o["source_id"] for o in self.observations}:
            self.db.require_source(source)

    def validate_citation(self, citation: Citation):
        if citation.evidence_id not in self.by_id:
            raise DomainError("reference_outside_batch")
        pinned = self.by_id[citation.evidence_id]
        if citation.revision != pinned["revision"] or citation.source_id != pinned["source_id"]:
            raise DomainError("reference_revision_mismatch")
        record = self.db.inspect(citation.evidence_id, citation.revision)
        text = record["content"].get(citation.field, "")
        if not 0 <= citation.start < citation.end <= len(text):
            raise DomainError("reference_span_invalid")
        excerpt = text[citation.start : citation.end]
        if (
            citation.excerpt != excerpt
            or hashlib.sha256(excerpt.encode()).hexdigest() != citation.sha256
        ):
            raise DomainError("reference_excerpt_mismatch")
        return citation.model_dump(mode="json") | {
            "source": self.source_descriptor(citation.evidence_id),
            "evidence_kind": pinned["kind"],
            "origin_label": "图片识别文字（待核验）"
            if citation.field == "media_text"
            else "评论"
            if pinned["kind"] == "comment"
            else "标题"
            if citation.field == "title"
            else "正文",
        }

    def source_descriptor(self, evidence_id):
        """Resolve a citation to its root note without model-authored provenance."""
        observation = self.by_id[evidence_id]
        root = (
            observation
            if observation["kind"] == "note"
            else self.by_id.get(observation["parent_id"], observation)
        )
        content = root["content"]
        return {
            "evidence_id": root["id"],
            "revision": root["revision"],
            "title": content.get("title") or content.get("external_id") or "未命名笔记",
            "public_url": public_http_url(content.get("locator")),
            "observed_at": root["observed_at"],
        }

    def resolve(self, ref_id):
        if ref_id not in self.catalog:
            raise DomainError("unknown_reference_id")
        return self.validate_citation(self.catalog[ref_id])

    def prompt_catalog(self, ids=None):
        self.check_permissions()
        return [
            {
                "ref_id": key,
                "evidence_id": ref.evidence_id,
                "kind": self.by_id[ref.evidence_id]["kind"],
                "parent_id": self.by_id[ref.evidence_id]["parent_id"],
                "field": ref.field,
                "span": {"start": ref.start, "end": ref.end},
                "machine_extracted": ref.field == "media_text",
                "text": ref.excerpt,
                "text_truncated": self.max_chars
                < len(self.by_id[ref.evidence_id]["content"][ref.field]),
                "is_author_reply": self.by_id[ref.evidence_id]["content"]["is_author_reply"],
                "synthetic": self.by_id[ref.evidence_id]["content"]["synthetic"],
                "author_pseudonym": self.by_id[ref.evidence_id]["content"]["author_pseudonym"],
                "observed_at": self.by_id[ref.evidence_id]["observed_at"],
                "sampling_and_coverage": self.by_id[ref.evidence_id]["samples"],
            }
            for key, ref in self.catalog.items()
            if ids is None or ref.evidence_id in ids
        ]

    def dispatch(self, name, evidence_id=None):
        if name not in self.ALLOWED_TOOLS:
            raise DomainError("tool_not_allowed")
        self.check_permissions()
        self.tool_calls.append(name)
        if name == "compare_metrics":
            ranking = self.metric_context or rank_observations(self.observations)
            rows = [row for row in ranking["notes"] if row["evidence_id"] in self.by_id]
            cohort_ids = {row.get("cohort_id") for row in rows}
            return {
                **ranking,
                "notes": rows,
                "cohorts": [c for c in ranking["cohorts"] if c["id"] in cohort_ids],
            }
        if evidence_id not in self.by_id or self.by_id[evidence_id]["kind"] != "note":
            raise DomainError("note_outside_batch")
        if name == "get_note_evidence":
            return self.prompt_catalog({evidence_id})
        return self.prompt_catalog(
            {o["id"] for o in self.observations if o["parent_id"] == evidence_id}
        )
