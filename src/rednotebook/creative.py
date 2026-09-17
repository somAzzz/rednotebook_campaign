"""Immutable, evidence-bound editorial drafts and explicitly signed delivery packages."""

import base64
import hashlib
import io
import json
import shutil
import tempfile
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont
from pydantic import Field

from rednotebook.domain.models import Contract, Text
from rednotebook.errors import DomainError
from rednotebook.research.store import read_run
from rednotebook.util import canonical, digest, stamp


class Page(Contract):
    title: Text = Field(max_length=100)
    body: Text = Field(max_length=900)
    asset_id: str | None = None


class Editorial(Contract):
    title: Text = Field(max_length=100)
    body: Text = Field(max_length=5000)
    pages: list[Page] = Field(min_length=6, max_length=6)
    disclosure: Text = "研究草案；样本观察不代表普遍规律。"


def load_bundle(db, bundle_id, version):
    row = db.conn.execute(
        "SELECT * FROM bundles WHERE id=? AND version=?", (bundle_id, version)
    ).fetchone()
    if not row:
        raise DomainError("bundle_not_found")
    if row["state"] == "revoked" or row["payload"] is None:
        raise DomainError("bundle_revoked")
    for source in bundle_sources(db, bundle_id, version):
        db.require_source(source)
    for run in bundle_runs(db, bundle_id, version):
        read_run(db, run)
    payload = json.loads(row["payload"])
    if digest(payload) != row["content_hash"]:
        raise DomainError("bundle_hash_mismatch")
    return dict(row) | {"payload": payload}


def bundle_sources(db, bundle_id, version):
    return [
        r[0]
        for r in db.conn.execute(
            "SELECT source_id FROM bundle_sources WHERE bundle_id=? AND version=?",
            (bundle_id, version),
        )
    ]


def bundle_runs(db, bundle_id, version):
    return [
        r[0]
        for r in db.conn.execute(
            "SELECT run_id FROM bundle_runs WHERE bundle_id=? AND version=?",
            (bundle_id, version),
        )
    ]


def parse_editorial(payload, value):
    if payload.get("kind") == "campaign":
        from rednotebook.campaign_contracts import PostDraft

        return PostDraft.model_validate(value)
    return Editorial.model_validate(value)


def propose(db, run_id):
    from rednotebook.workspace import findings_for_draft

    report = findings_for_draft(db, run_id)
    findings = report.get("findings", [])
    if not findings:
        raise DomainError("research_has_no_findings")
    row = db.conn.execute(
        "SELECT b.payload FROM briefs b JOIN research_runs r "
        "ON b.id=r.brief_id AND b.version=r.brief_version WHERE r.id=?",
        (run_id,),
    ).fetchone()
    brief = json.loads(row[0])
    claims = [f["claim"] for f in findings]
    evidence = [c for f in findings for c in f.get("support", [])]
    directions = [
        {"angle": angle, "validation_target": target, "status": "hypothesis"}
        for angle, target in [
            ("问题观察", "核对观察是否被独立样本支持"),
            ("解释机制", "寻找其他解释与反例"),
            ("操作方法", "作者实测方法并记录适用条件"),
        ]
    ]
    pages = [
        Page(title="从公开笔记提出一个问题", body=brief["research_question"]),
        Page(title="样本中的观察（待核验）", body=claims[0][:900]),
        Page(title="证据与范围", body="这些观察仅来自选定样本，引用见来源说明。"),
        Page(title="反例与其他解释", body="逐条核对研究报告的反例与局限；未发现不等于不存在。"),
        Page(title="下一步如何验证", body="扩大独立作者样本，保留不支持该观察的记录。"),
        Page(title="适用边界", body="这是研究与选题草案，不包含未经实测的产品承诺。"),
    ]
    editorial = Editorial(
        title=brief["research_question"][:100],
        body="公开笔记研究草案。所有观察均需审核支持关系。",
        pages=pages,
    )
    payload = {
        "editorial": editorial.model_dump(),
        "directions": directions,
        "verified_facts": [f for f in brief["facts"] if f["status"] == "verified"],
        "gaps": ["人工核验支持关系", "作者原创素材或明确复用许可"],
        "citations": evidence,
        "finding_feedback": [
            {"finding_id": f.get("id"), "feedback": f.get("user_feedback")} for f in findings
        ],
        "synthetic": brief["synthetic"],
        "similarity": [],
        "generation": "deterministic_editorial_scaffold",
    }
    return save_bundle(db, str(uuid4()), run_id, payload)


def save_bundle(db, bundle_id, run_id, payload, *, source_ids=(), run_ids=()):
    if db.conn.execute(
        "SELECT 1 FROM bundles WHERE id=? AND state='revoked'", (bundle_id,)
    ).fetchone():
        raise DomainError("bundle_revoked")
    runs = set(run_ids) | ({run_id} if run_id else set())
    sources = set(source_ids)
    # Dependencies are monotonic across a lineage: editing prose cannot launder sources.
    for old in db.conn.execute("SELECT version FROM bundles WHERE id=?", (bundle_id,)):
        sources.update(bundle_sources(db, bundle_id, old[0]))
        runs.update(bundle_runs(db, bundle_id, old[0]))
    for run in runs:
        read_run(db, run)
        sources.update(
            r[0]
            for r in db.conn.execute(
                "SELECT source_id FROM research_sources WHERE run_id=?", (run,)
            )
        )
    for source in sources:
        db.require_source(source, "storage")
        db.require_source(source)
    if run_id is None and payload.get("kind") != "campaign":
        raise DomainError("bundle_context_required")
    hashed = digest(payload)
    with db.conn:
        previous = db.conn.execute(
            "SELECT version,content_hash FROM bundles WHERE id=? ORDER BY version DESC LIMIT 1",
            (bundle_id,),
        ).fetchone()
        if previous and previous["content_hash"] == hashed:
            return load_bundle(db, bundle_id, previous[0])
        version = db.conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM bundles WHERE id=?", (bundle_id,)
        ).fetchone()[0]
        db.conn.execute(
            "INSERT INTO bundles VALUES (?,?,?,?,?,?,?,?,?)",
            (
                bundle_id,
                version,
                run_id,
                hashed,
                canonical(payload),
                "draft",
                None,
                None,
                stamp(db.clock()),
            ),
        )
        db.conn.executemany(
            "INSERT INTO bundle_sources VALUES (?,?,?)", [(bundle_id, version, s) for s in sources]
        )
        db.conn.executemany(
            "INSERT INTO bundle_runs VALUES (?,?,?)", [(bundle_id, version, r) for r in runs]
        )
    return load_bundle(db, bundle_id, version)


def revise(db, bundle_id, version, editorial):
    row = load_bundle(db, bundle_id, version)
    payload = row["payload"]
    if any(p.asset_id and p.asset_id not in payload.get("assets", {}) for p in editorial.pages):
        raise DomainError("page_asset_not_found")
    editorial = parse_editorial(payload, editorial.model_dump())
    payload["editorial"] = editorial.model_dump()
    if payload.get("kind") == "campaign":
        from rednotebook.campaign import invalidate_confirmation, validate_output

        invalidate_confirmation(payload)
        validate_output(payload)
    text = editorial.body + "".join(p.body for p in editorial.pages)
    payload["similarity"] = [
        {"evidence_id": c.get("evidence_id"), "ratio": round(ratio, 3), "review_required": True}
        for c in payload["citations"]
        if (ratio := SequenceMatcher(None, text, c.get("excerpt", "")).ratio()) > 0.35
    ]
    return save_bundle(db, bundle_id, row["run_id"], payload)


def review(db, bundle_id, version, expected_hash, reviewer):
    row = load_bundle(db, bundle_id, version)
    if expected_hash != row["content_hash"] or not reviewer.strip():
        raise DomainError("review_hash_or_reviewer_invalid")
    if row["payload"].get("kind") == "campaign":
        from rednotebook.campaign import require_ready

        require_ready(db, row)
    with db.conn:
        db.conn.execute(
            "UPDATE bundles SET state='approved',reviewer=?,approved_hash=? "
            "WHERE id=? AND version=?",
            (reviewer, expected_hash, bundle_id, version),
        )
    return load_bundle(db, bundle_id, version)


def font_path():
    for file in [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]:
        if Path(file).is_file():
            return file
    raise DomainError("chinese_font_missing")


def wrap(draw, text, font, width):
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for char in paragraph:
            if draw.textlength(line + char, font=font) > width:
                lines.append(line)
                line = char
            else:
                line += char
        lines.append(line)
    return lines


def render_page(page, target, index, preview, assets=None, campaign=False):
    im = Image.new("RGB", (1080, 1620), "#f6f3eb")
    draw = ImageDraw.Draw(im)
    font = font_path()
    draw.rectangle((70, 80, 1010, 96), fill="#256e60")
    draw.text(
        (76, 128),
        f"REDNOTEBOOK   /   {index:02}",
        font=ImageFont.truetype(font, 26),
        fill="#256e60",
    )
    y = 220
    for text, initial, max_height in [
        (page.title, 68, 330),
        (page.body, 40, 440 if page.asset_id else 800),
    ]:
        for size in range(initial, 23, -2):
            f = ImageFont.truetype(font, size)
            lines = wrap(draw, text, f, 920)
            if len(lines) * (size + 18) <= max_height:
                break
        else:
            raise DomainError("text_overflow")
        for line in lines:
            draw.text((80, y), line, font=f, fill="#172e29")
            y += size + 18
        y += 60
    if page.asset_id:
        asset = (assets or {}).get(page.asset_id)
        if not asset:
            raise DomainError("page_asset_not_found")
        raw = base64.b64decode(asset["data"], validate=True)
        if hashlib.sha256(raw).hexdigest() != asset["sha256"]:
            raise DomainError("asset_hash_mismatch")
        with Image.open(io.BytesIO(raw)) as original:
            original = original.convert("RGB")
            original.thumbnail((920, 500))
            im.paste(original, ((1080 - original.width) // 2, 930))
    footer = "预览 · 未批准" if preview else ("" if campaign else "研究观察 · 非普遍结论")
    draw.text((80, 1490), footer, font=ImageFont.truetype(font, 32), fill="#9c4434")
    im.save(target, format="PNG")


def export(db, bundle_id, version, preview=False):
    row = load_bundle(db, bundle_id, version)
    if not preview and (row["state"] != "approved" or row["approved_hash"] != row["content_hash"]):
        raise DomainError("exact_version_approval_required")
    if not preview and row["payload"].get("kind") == "campaign":
        from rednotebook.campaign import require_ready

        require_ready(db, row)
    for source in bundle_sources(db, bundle_id, version):
        db.require_source(source, "excerpt_export")
    root = db.path.resolve().parent / "managed-exports"
    if root.is_symlink():
        raise DomainError("managed_export_path_invalid")
    root.mkdir(mode=0o700, exist_ok=True)
    name = f"{bundle_id}-v{version}-" + ("preview" if preview else "approved")
    target = root / name
    if target.is_symlink():
        raise DomainError("managed_export_path_invalid")
    if target.exists():
        registered = db.conn.execute(
            "SELECT manifest_hash FROM managed_exports WHERE path=?", (str(target),)
        ).fetchone()
        if (
            not registered
            or not registered[0]
            or hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest() != registered[0]
        ):
            raise DomainError("export_tampered")
        manifest = json.loads((target / "manifest.json").read_text())
        for name, hashed in manifest["files"].items():
            if (
                (target / name).is_symlink()
                or Path(name).name != name
                or hashlib.sha256((target / name).read_bytes()).hexdigest() != hashed
            ):
                raise DomainError("export_tampered")
        if manifest["bundle_hash"] != row["content_hash"]:
            raise DomainError("export_tampered")
        return {"path": str(target), "manifest": manifest, "reused": True}
    temp = Path(tempfile.mkdtemp(prefix=".render-", dir=root))
    try:
        editorial = parse_editorial(row["payload"], row["payload"]["editorial"])
        for i, page in enumerate(editorial.pages, 1):
            render_page(
                page,
                temp / f"{i:02}.png",
                i,
                preview,
                row["payload"].get("assets"),
                row["payload"].get("kind") == "campaign",
            )
        (temp / "body.txt").write_text(editorial.body + "\n\n" + editorial.disclosure)
        if row["payload"].get("kind") == "campaign":
            (temp / "title.txt").write_text(editorial.title)
        references = []
        seen_references = set()
        for citation in row["payload"]["citations"]:
            key = digest(citation)
            if key in seen_references:
                continue
            seen_references.add(key)
            record = db.inspect(citation["evidence_id"], citation["revision"])
            url = urlsplit(record["content"].get("locator", ""))
            locator = (
                urlunsplit((url.scheme, url.netloc, url.path, "", ""))
                if url.scheme in ("https", "http")
                else None
            )
            references.append({"citation": citation, "locator": locator})
        (temp / "sources.json").write_text(canonical(references))
        for asset_id, asset in row["payload"].get("assets", {}).items():
            raw = base64.b64decode(asset["data"], validate=True)
            if hashlib.sha256(raw).hexdigest() != asset["sha256"]:
                raise DomainError("asset_hash_mismatch")
            (temp / f"asset-{asset_id}.bin").write_bytes(raw)
        manifest = {
            "bundle_id": bundle_id,
            "version": version,
            "bundle_hash": row["content_hash"],
            "preview": preview,
            "synthetic": row["payload"]["synthetic"],
            "dimensions": [1080, 1620],
            "page_count": len(editorial.pages),
            "renderer": "Pillow",
            "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in temp.iterdir()},
        }
        (temp / "manifest.json").write_text(canonical(manifest))
        load_bundle(db, bundle_id, version)
        with db.conn:
            db.conn.execute(
                "INSERT OR REPLACE INTO managed_exports VALUES (?,?,?,?)",
                (
                    bundle_id,
                    version,
                    str(target),
                    hashlib.sha256((temp / "manifest.json").read_bytes()).hexdigest(),
                ),
            )
        temp.rename(target)
        return {"path": str(target), "manifest": manifest, "reused": False}
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def purge_bundle(db, bundle_id, version):
    root = db.path.resolve().parent / "managed-exports"
    for item in db.conn.execute(
        "SELECT path FROM managed_exports WHERE bundle_id=? AND version=?", (bundle_id, version)
    ):
        path = Path(item[0])
        if path.parent.resolve() != root or path.is_symlink():
            raise DomainError("managed_export_path_invalid")
        if path.exists():
            shutil.rmtree(path)
    with db.conn:
        db.conn.execute(
            "DELETE FROM managed_exports WHERE bundle_id=? AND version=?", (bundle_id, version)
        )
        db.conn.execute(
            "UPDATE bundles SET state='revoked',payload=NULL,reviewer=NULL,approved_hash=NULL "
            "WHERE id=? AND version=?",
            (bundle_id, version),
        )
        db.conn.execute(
            "DELETE FROM outcomes WHERE bundle_id=? AND bundle_version=?", (bundle_id, version)
        )


def purge_source(db, source_id):
    rows = db.conn.execute(
        "SELECT bundle_id,version FROM bundle_sources WHERE source_id=?", (source_id,)
    ).fetchall()
    for row in rows:
        purge_bundle(db, *tuple(row))
    with db.conn:
        db.conn.execute("DELETE FROM outcomes WHERE source_id=?", (source_id,))
        db.conn.execute("DELETE FROM media_cache WHERE source_id=?", (source_id,))


def add_asset(db, bundle_id, version, path, owner, rights_ref):
    row = load_bundle(db, bundle_id, version)
    if not owner.strip() or not rights_ref.strip():
        raise DomainError("asset_rights_required")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise DomainError("asset_too_large")
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        if im.width * im.height > 32_000_000:
            raise DomainError("asset_pixel_budget_exceeded")
        im.verify()
    hashed = hashlib.sha256(raw).hexdigest()
    payload = row["payload"]
    assets = payload.setdefault("assets", {})
    if len(assets) >= (20 if payload.get("kind") == "campaign" else 6) and hashed not in assets:
        raise DomainError("asset_budget_exceeded")
    if payload.get("kind") == "campaign":
        from rednotebook.campaign import invalidate_confirmation

        invalidate_confirmation(payload)
    assets[hashed] = {
        "sha256": hashed,
        "owner": owner,
        "rights_ref": rights_ref,
        "data": base64.b64encode(raw).decode(),
    }
    return save_bundle(db, bundle_id, row["run_id"], payload)
