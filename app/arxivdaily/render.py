"""Render immutable public digests and size-bounded ServerChan messages."""
from __future__ import annotations

from datetime import date
from urllib.parse import urlparse

from .models import DigestItem, Paper, parse_time
from zoneinfo import ZoneInfo


def _day(value: date | str) -> str:
    result = value.isoformat() if isinstance(value, date) else str(value)
    date.fromisoformat(result)
    return result


def _title(item: DigestItem) -> str:
    # A title is untrusted text; keep it on one Markdown line.
    return " ".join(item.paper.title.split()).replace("[", "\\[").replace("]", "\\]")


def _url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("archive URL must be a public HTTPS URL")
    return url


def render_item(item: DigestItem) -> str:
    # Old saved items carry no module list; adapt them once at the rendering boundary.
    modules = set(item.modules if item.modules is not None else
                  ["title", "source", "llm_summary"] if item.tier == "full" else
                  ["title", "title_translation", "abstract", "abstract_translation", "source"] if item.tier == "translation" else
                  ["title", "source"])
    lines = [f"### {_title(item)}" if "title" in modules else f"### arXiv {item.paper.version_id}"]
    if "title_translation" in modules:
        if item.translation is None:
            raise ValueError("title translation module requires translated title")
        lines.append(f"**中文标题：** {item.translation.title_zh}")
    if "abstract" in modules:
        lines.append(f"**摘要：** {item.paper.abstract}")
    if "abstract_translation" in modules:
        if item.translation is None or not item.translation.abstract_zh.strip():
            raise ValueError("abstract translation module requires translated abstract")
        lines.append(f"**摘要翻译：** {item.translation.abstract_zh}")
    if item.translation and item.translation.fallback_provider == "google_free":
        lines.append("**翻译来源：** 谷歌备用翻译")
    if "llm_summary" in modules:
        if item.summary is not None:
            s = item.summary
            lines.extend([f"**背景：** {s.background}", f"**贡献：** {s.contribution}",
                          f"**方法：** {s.method}", f"**实验：** {s.experiments}", f"**结论：** {s.conclusion}"])
        elif item.tier == "light":
            lines.append("**LLM总结：** 轻量档未生成全文总结。")
        else:
            raise ValueError("LLM summary module requires validated summary")
    if "evidence" in modules:
        if item.summary is not None:
            lines.append("**证据链：**\n" + "\n".join(
                f"- {e.field} · [{e.locator}]({_url(e.source_url)})：{e.quote}" for e in item.summary.evidence))
        elif item.tier == "light":
            lines.append("**证据链：** 轻量档未生成全文证据。")
        else:
            raise ValueError("evidence module requires validated summary")
    if "source" in modules:
        lines.append(f"**来源：** [arXiv {item.paper.version_id}]({item.paper.url})")
    if "screening_routes" in modules:
        routes = "、".join(dict.fromkeys(item.rule_hits)) or "无初筛命中记录"
        lines.append(f"**初筛路径：** {routes}")
    return "\n\n".join(lines)


def _groups(items: list[DigestItem]):
    if any(i.tier not in {"full", "light", "translation"} for i in items):
        raise ValueError("unknown digest tier")
    for recovery in (False, True):
        for tier, label in (("translation", "论文条目"), ("full", "完整档"), ("light", "轻量档")):
            group = [i for i in items if i.tier == tier and bool(i.recovery_of) == recovery]
            if group:
                yield "## " + ("补发" if recovery else "") + label, group


def render_paper_index(records: dict) -> str:
    def cell(text):
        return " ".join(text.split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\\", "&#92;")
    lines = ["# arXiv 论文长期索引", "", "日期为北京时间首次收集日期；按 arXiv ID 去重，包含规则命中的候选论文（不等同于已推送或已通过终筛）。", "",
             "| 日期 | 英文标题 | 中文标题 | 链接 |", "| --- | --- | --- | --- |"]
    for record in sorted(records.values(), key=lambda r: (r["first_discovered"], r["paper"]["version_id"]), reverse=True):
        paper = Paper.from_dict(record["paper"])
        day = parse_time(record["first_discovered"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        translation = record.get("translation", {})
        title_zh = translation.get("title_zh") or record.get("title_zh") or "待翻译"
        if translation.get("fallback_provider") == "google_free":
            title_zh += "（谷歌备用翻译）"
        lines.append(f"| {day} | {cell(paper.title)} | {cell(title_zh)} | [arXiv {paper.version_id}]({paper.url}) |")
    return "\n".join(lines) + "\n"


def render_archive(date: date | str, items: list[DigestItem], notes=None) -> str:
    """Return the complete, public Markdown snapshot."""
    parts = [f"# arXiv 每日简报 · {_day(date)}"]
    if not items:
        parts.append("今天未发现新增工作（本次检查未发现新增符合关注条件的论文）。")
    for heading, group in _groups(items):
        parts.append(heading)
        if group[0].recovery_of:
            parts.append("上一轮投递未确认，以下条目本次可能重复送达。")
        parts.append("\n\n".join(render_item(item) for item in group))
    if notes:
        notes = [notes] if isinstance(notes, str) else notes
        parts.extend(["## 运行备注", "\n".join(f"- {str(note)}" for note in notes)])
    return "\n\n".join(parts).rstrip() + "\n"


def server_title(date: date | str, label: str = "") -> str:
    base = f"arXiv 每日简报 {_day(date)}"
    label = label if isinstance(label, str) else ""
    if label and len(label) <= 32:
        return f"{base} · {label}"
    return base


def render_notification(
    date: date | str, items: list[DigestItem], archive_url: str, max_bytes: int = 30000,
    *, index_url: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Fit whole paper blocks; move overflow to the immutable public archive."""
    day, archive_url = _day(date), _url(archive_url)
    if index_url:
        index_url = _url(index_url)
    if not 0 < max_bytes <= 32768:
        raise ValueError("message byte cap must be within ServerChan's 32 KiB limit")
    ids = [i.paper.base_id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate base arXiv ID in digest")
    groups = list(_groups(items))
    blocks = {i.paper.base_id: render_item(i) for i in items}
    inline = set(ids)
    short_labels = set()
    fallback_count = sum(bool(i.translation and i.translation.fallback_provider == "google_free") for i in items)

    def compose():
        modes = {}
        lines = [f"# arXiv 每日简报 · {day}"]
        if not items:
            lines.append("今天未发现新增工作（本次检查未发现新增符合关注条件的论文）。")
        if any(i.recovery_of for i in items):
            lines.append("**补发：上一轮投递未确认，本次可能重复。**")
        if fallback_count:
            lines.append(f"**翻译来源：** 本期有 {fallback_count} 篇使用谷歌备用翻译。")
        for heading, group in groups:
            lines.append(heading)
            omitted = 0
            for item in group:
                key = item.paper.base_id
                if key in inline:
                    lines.append(blocks[key])
                    modes[key] = {"full": "rendered_inline_full", "light": "light_title_link", "translation": "rendered_inline_translation"}[item.tier]
                elif item.tier == "light":
                    omitted += 1
                    modes[key] = "light_archive_only"
                else:
                    label = f"arXiv {key}" if key in short_labels or (item.modules is not None and "title" not in item.modules) else _title(item)
                    lines.append(f"- [{label}]({archive_url})：所选内容见公开存档。")
                    modes[key] = "archive_link_only" if item.tier == "full" else "translation_archive_link_only"
            if omitted:
                lines.append(f"另有 {omitted} 篇轻量档仅列于公开存档。")
        lines.append(f"[完整日报与长期存档]({archive_url})")
        if index_url:
            lines.append(f"[论文长期索引]({index_url})")
        return "\n\n".join(lines).rstrip() + "\n", modes

    body, modes = compose()
    for tier in ("translation", "light", "full"):
        for item in reversed(items):
            if len(body.encode("utf-8")) <= max_bytes:
                break
            if item.tier == tier:
                inline.discard(item.paper.base_id)
                body, modes = compose()
    for item in reversed(items):
        if len(body.encode("utf-8")) <= max_bytes:
            break
        if item.tier != "light":
            short_labels.add(item.paper.base_id)
            body, modes = compose()
    if len(body.encode("utf-8")) > max_bytes:
        raise ValueError("message metadata alone exceeds byte cap")
    return body, modes
