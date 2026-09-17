"""Plain Markdown report. All model conclusions remain explicitly unreviewed."""

import re

from rednotebook.provenance import public_http_url


def text(value):
    # Disable model-supplied HTML, links and Markdown images in the report UI.
    value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()!#|])", r"\\\1", value)


def source_line(citation):
    source = citation.get("source") or {}
    label = text(source.get("title") or citation["evidence_id"])
    url = public_http_url(source.get("public_url"))
    rendered = f"[{label}](<{url}>)" if url else f"{label}（无公开链接）"
    return (
        f"  来源：{rendered} / 证据 {text(citation['evidence_id'])} / "
        f"修订 {citation['revision']} / {citation['field']}[{citation['start']}:{citation['end']}]"
    )


def markdown(report):
    if "findings" not in report:
        return f"研究 {text(report['run_id'])}：{text(report['state'])}\n"
    q = report["quality"]
    lines = [
        "# 研究草案（需要人工核验）",
        "",
        f"状态：{text(report['state'])}；模型：{text(report['metadata']['model']['model'])}",
        f"资料：{q['notes']} 篇笔记、{q['comments']} 条评论；本轮选择 {report['selected_notes']} 篇、{report['selected_comments']} 条。",
        f"合成数据：{'是，仅用于工程验证' if report['metadata']['synthetic'] else '否'}。",
        "引用已做存在性和片段匹配校验，但结论是否被证据支持仍需人工审核；不代表全平台需求或因果关系。",
        "含机器提取的图片文字，仍待人工核验。"
        if report.get("visual_analysis") == "imported_machine_extractions"
        else "未做视觉分析。",
        "",
    ]
    for index, finding in enumerate(report["findings"], start=1):
        lines.extend(
            [
                f"## {index}. {text(finding['category'])}：待验证假设",
                "",
                text(finding["claim"]),
                "",
                f"局限：{text(finding['limitation'])}",
                f"反例检查：{text(finding['counter_search'])}",
                "",
            ]
        )
        for label, key in [("支持", "support"), ("反例", "counter")]:
            for cite in finding[key]:
                lines.extend(
                    [
                        f"- {label}（{text(cite.get('origin_label', cite['field']))}）：{text(cite['excerpt'])}",
                        source_line(cite),
                        "",
                    ]
                )
    if report["gaps"]:
        lines.extend(["## 材料缺口（模型建议，待核验）", ""])
        lines.extend("- " + text(gap) for gap in report["gaps"])
    if report["errors"]:
        lines.extend(["", "## 未完成原因", ""])
        lines.extend("- " + text(error["code"]) for error in report["errors"])
    return "\n".join(lines) + "\n"
