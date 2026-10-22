"""
Blog Writer v2 - writes a real, long-form article instead of filling a template.

Pipeline (each step is streamed to the UI as an event):
  1. Read the brand's own website  -> brand facts (what they sell, proof points)
  2. Research the topic            -> facts with source URLs (Perplexity, if a key is set)
  3. Plan                          -> article type, title, meta, 6-10 sections, FAQ
  4. Write                         -> intro, every section and the conclusion, in parallel
  5. Check                         -> keyword report, GEO checklist, facts to verify, JSON-LD

Grounding rules: the writer may only state specific facts that come from the brand's
site or the research notes. Anything else that needs a number or a source becomes a
[STAT: ...] / [SOURCE: ...] placeholder that the UI asks a human to confirm.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterator, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from .content_advisor import (FACT_PLACEHOLDER_RE, _slugify, _template_blog, build_json_ld,
                              keyword_report, quick_geo_check)
from .llm_writer import TextGenerator
from .query_generator import brand_from_url

UA = {"User-Agent": "Mozilla/5.0 (AI Visibility Analyzer; academic project)"}

ARTICLE_TYPES = {
    "listicle": {
        "label": "Listicle",
        "structure": "Short intro. Then numbered H2 sections (\"1. ...\", \"2. ...\"), one per item. Each item: "
                     "what it is, who it suits, and a short labelled list such as \"Best for:\" or \"Watch out for:\". "
                     "Finish with a comparison table of all items, then the conclusion.",
    },
    "honest_assessment": {
        "label": "Honest assessment",
        "structure": "Intro that admits no option fits everyone. A fair \"Where it shines\" section first. Then numbered "
                     "H2 sections, each a situation where it is NOT the right choice, with short labelled lists like "
                     "\"Warning signs:\" and \"Better alternatives:\". Then a quick decision table (Situation | Good fit? | "
                     "Consider instead), a section on combining options, and the conclusion.",
    },
    "comparison": {
        "label": "Comparison",
        "structure": "Intro with a one-paragraph verdict. A comparison table near the top. Then one H2 per criterion "
                     "(price, quality, use cases, etc.) comparing options fairly. A \"Who should choose which\" section, "
                     "then the conclusion.",
    },
    "how_to": {
        "label": "How-to guide",
        "structure": "Intro stating the outcome. A \"What you need\" section. Then numbered step sections with concrete "
                     "actions. A \"Common mistakes to avoid\" section, then the conclusion.",
    },
    "buyers_guide": {
        "label": "Buyer's guide",
        "structure": "Intro. \"What to look for\" sections, one per buying criterion, each explaining why it matters and "
                     "how to check it. A comparison table of options. A checklist section, then the conclusion.",
    },
}

STYLE_RULES = """Writing style:
- Write like an experienced practitioner giving honest advice, not like marketing copy.
- Plain English. Short paragraphs of 2-4 sentences. Concrete examples over abstractions.
- Open each section with one or two sentences that directly answer its heading.
- Use short labelled lists where they help scanning (e.g. "Warning signs:", "Best for:"), and tables for comparisons.
- Be fair to competitors: name their strengths honestly and never disparage them. Balanced content is more
  trustworthy to readers and more likely to be quoted by AI assistants.
- Mention the brand only where it genuinely fits, never as a hard sell.
- Avoid filler such as "in today's fast-paced world", "delve", "unlock", "game-changer", "it's important to note".

Facts (strict):
- Only state specific facts, prices, numbers, certifications or claims that appear in BRAND FACTS or RESEARCH NOTES.
- When you use a research note, cite it inline as a markdown link, e.g. ([source](https://...)).
- Where a number, study or quote would help but you don't have one, write a placeholder instead:
  [STAT: what data is needed], [SOURCE: what to cite] or [QUOTE: who should be quoted]. Never invent one."""


# --------------------------------------------------------------------------- helpers
def pick_article_type(topic: str) -> str:
    t = topic.lower()
    if re.search(r"\bvs\.?\b|versus|compare|comparison|difference", t):
        return "comparison"
    if re.search(r"^how (do|to|can)|step|guide to|set up|start", t):
        return "how_to"
    if re.search(r"when not|is .* worth|should i|honest|pros and cons|downside|anything better|alternative", t):
        return "honest_assessment"
    if re.search(r"how (do i )?choose|what to look for|buy|affordable|cheap|budget", t):
        return "buyers_guide"
    return "listicle"


def _page_text(url: str, limit: int = 3500):
    from bs4 import BeautifulSoup
    html = requests.get(url, headers=UA, timeout=15).text
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"})
    parts = [title, meta.get("content", "") if meta else ""]
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        t = el.get_text(" ", strip=True)
        if 3 < len(t) < 400 and t not in parts:
            parts.append(t)
    links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]
    return "\n".join(p for p in parts if p)[:limit], links


def gather_brand_context(url: str, gen: TextGenerator) -> Dict:
    """Read the homepage plus up to 3 key internal pages (about, products, pricing)."""
    try:
        home, links = _page_text(url)
    except Exception as e:
        reason = "site unreachable" if isinstance(e, requests.ConnectionError) else type(e).__name__
        return {"summary": "", "products": [], "differentiators": [], "audience": "", "proof_points": [],
                "pages_read": [], "error": f"Couldn't read {urlparse(url).netloc or url} ({reason}). "
                                          "The article was written without brand facts."}
    host = urlparse(url).netloc
    wanted = re.compile(r"about|product|shop|service|pricing|feature|solution|story", re.I)
    extra, seen = [], {url.rstrip("/")}
    for link in links:
        if urlparse(link).netloc == host and wanted.search(urlparse(link).path) and link.rstrip("/") not in seen:
            seen.add(link.rstrip("/"))
            extra.append(link)
        if len(extra) == 3:
            break
    texts, pages = [home], [url]
    for link in extra:
        try:
            texts.append(_page_text(link, 2000)[0])
            pages.append(link)
        except Exception:
            pass
    raw = "\n\n---\n\n".join(texts)[:8000]

    if not gen.available:
        first = [l for l in raw.splitlines() if l.strip()][:4]
        return {"summary": " ".join(first[:2]), "products": [], "differentiators": [], "audience": "",
                "proof_points": [], "pages_read": pages}
    try:
        facts = gen.complete_json(
            "You extract facts about a business from its own website text. Use ONLY the text given. "
            "If something isn't stated, leave it empty. Return JSON only.",
            f"""Website text from {url}:
{raw}

Return: {{"summary": "1-2 sentences on what the business does",
 "products": ["named products or services, with prices if stated"],
 "differentiators": ["what they claim makes them different"],
 "audience": "who they serve",
 "proof_points": ["certifications, awards, customer numbers, reviews - only if stated"]}}""",
            temperature=0.1, max_tokens=1200)
        facts["pages_read"] = pages
        return facts
    except Exception as e:
        return {"summary": raw[:300], "products": [], "differentiators": [], "audience": "", "proof_points": [],
                "pages_read": pages, "error": str(e)}


def research_topic(topic: str, keywords: List[str], region: str, industry: str) -> Dict:
    """Web research with sources via Perplexity. Returns {"notes": str, "sources": [{n, url}]}."""
    key = os.getenv("PERPLEXITY_API_KEY")
    if not key:
        return {"notes": "", "sources": [], "used": False}
    prompt = (f"I'm writing a blog article answering: \"{topic}\". Related keywords: {', '.join(keywords)}."
              f"{f' Market: {region}.' if region else ''}{f' Industry: {industry}.' if industry else ''}\n"
              "List 8-12 specific, verifiable facts a writer could use: statistics, guidelines from official bodies, "
              "price ranges, common buyer concerns, and how the main options differ. One fact per line, each with its "
              "citation number like [1]. No introduction.")
    try:
        r = requests.post("https://api.perplexity.ai/chat/completions", timeout=90,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": os.getenv("PERPLEXITY_MODEL", "sonar"),
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        data = r.json()
        urls = data.get("citations") or [s.get("url") for s in data.get("search_results", []) if s.get("url")]
        return {"notes": data["choices"][0]["message"]["content"],
                "sources": [{"n": i + 1, "url": u} for i, u in enumerate(urls)], "used": True}
    except Exception as e:
        return {"notes": "", "sources": [], "used": False, "error": str(e)}


def _context_block(brand: str, url: str, facts: Dict, research: Dict, competitors: List[str]) -> str:
    src = "\n".join(f"[{s['n']}] {s['url']}" for s in research.get("sources", []))
    return f"""BRAND: {brand} ({url})
BRAND FACTS (from the brand's own website):
{json.dumps({k: v for k, v in facts.items() if k not in ("pages_read", "error")}, indent=1, ensure_ascii=False)}

COMPETITORS (treat fairly): {', '.join(competitors) or 'none given'}

RESEARCH NOTES:
{research.get('notes') or '(none - use placeholders for any specific number or claim not in BRAND FACTS)'}
{('SOURCES (use these URLs when citing [n]):' + chr(10) + src) if src else ''}"""


# --------------------------------------------------------------------------- LLM steps
def plan_article(gen, topic, article_type, brand, primary, secondary, audience, tone, words, ctx) -> Dict:
    t = ARTICLE_TYPES[article_type]
    data = gen.complete_json(
        "You are a senior content strategist planning articles that rank in search and get quoted by AI "
        "assistants. Return JSON only.",
        f"""Plan a {t['label'].lower()} answering: "{topic}"
Audience: {audience or 'people researching this topic'}. Tone: {tone}. Total length: about {words} words.
Primary keyword: {primary}. Secondary keywords: {', '.join(secondary) or 'none'}.

Required structure for this article type:
{t['structure']}

{ctx}

Return:
{{"title": "specific, benefit-led, includes the primary keyword, under 70 characters",
 "meta_title": "under 60 characters", "meta_description": "under 155 characters, includes the primary keyword",
 "slug": "short-url-slug",
 "intro_angle": "the hook and the promise of the article in one sentence",
 "sections": [{{"heading": "H2 text", "goal": "what this section must achieve",
               "key_points": ["specific points to cover"], "format": "prose | list | table | steps",
               "words": 150}}],
 "conclusion_angle": "how to close, including a natural, low-key mention of {brand} if it fits",
 "faq": [{{"question": "...", "answer": "40-60 word direct answer"}}]}}
Use 6-10 sections whose words add up to about {int(words * 0.8)}. 4-6 FAQ items. Put the primary keyword in the
title and in at least one heading.""",
        temperature=0.5, max_tokens=3000)
    secs = [s for s in data.get("sections", []) if isinstance(s, dict) and s.get("heading")]
    if not secs:
        raise ValueError("Plan had no sections")
    data["sections"] = secs[:10]
    data["faq"] = [f for f in data.get("faq", []) if isinstance(f, dict) and f.get("question") and f.get("answer")][:6]
    return data


def _write(gen, kind, plan, article_type, primary, secondary, tone, audience, ctx, section=None) -> str:
    outline = "\n".join(f"- {s['heading']}" for s in plan["sections"])
    common = f"""ARTICLE: "{plan['title']}" ({ARTICLE_TYPES[article_type]['label']})
Audience: {audience or 'general readers'}. Tone: {tone}.
Primary keyword: {primary}. Secondary keywords: {', '.join(secondary) or 'none'} (use naturally, never stuff).
Full outline:
{outline}

{ctx}"""
    if kind == "intro":
        task = (f"Write the article's introduction: 2-4 short paragraphs, about 150 words. Hook: {plan.get('intro_angle', '')}. "
                f"Use the primary keyword in the first 100 words. End with a line starting \"**In short:**\" that "
                f"directly answers the title in 1-2 sentences. Do not add a heading.")
    elif kind == "conclusion":
        task = (f"Write the conclusion section, starting with \"## Conclusion\". About 120 words. "
                f"{plan.get('conclusion_angle', '')}. Summarise the key takeaway; end with one practical next step.")
    else:
        task = (f"Write ONLY this section, starting with \"## {section['heading']}\".\n"
                f"Goal: {section.get('goal', '')}\nCover: {'; '.join(section.get('key_points', []))}\n"
                f"Preferred format: {section.get('format', 'prose')}. Length: about {section.get('words', 180)} words.\n"
                f"Don't repeat what other sections in the outline will cover.")
    out = gen.complete(f"You are an expert writer for a {tone} blog.\n\n{STYLE_RULES}\n\nReturn Markdown only.",
                       f"{common}\n\nTASK:\n{task}", temperature=0.7, max_tokens=1800)
    return re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out.strip())


def rewrite_part(markdown: str, instruction: str, title: str, brand_url: str,
                 gen: Optional[TextGenerator] = None) -> str:
    gen = gen or TextGenerator()
    if not gen.available:
        raise RuntimeError("Rewriting needs a writer API key (WRITER_PROVIDER + its key).")
    out = gen.complete(
        f"You edit sections of a blog article.\n\n{STYLE_RULES}\n\nReturn only the rewritten Markdown.",
        f"""Article: "{title}" for {brand_from_url(brand_url)} ({brand_url}).
Keep the same heading line (if any) unless the instruction asks to change it. Keep existing source links and
placeholders unless the instruction says otherwise.

INSTRUCTION: {instruction}

SECTION:
{markdown}""", temperature=0.6, max_tokens=1800)
    return re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out.strip())


# --------------------------------------------------------------------------- pipeline
def _assemble(title, parts):
    return f"# {title}\n\n" + "\n\n".join(p["markdown"].strip() for p in parts if p["markdown"].strip()) + "\n"


def _finish(req, plan, parts, facts, research, mode, article_type) -> Dict:
    brand = brand_from_url(req["url"])
    keywords = [k for k in req.get("keywords", []) if k.strip()]
    primary, secondary = (keywords[0] if keywords else req["topic"]), keywords[1:]
    faq = plan.get("faq", [])
    if faq and not any(p["id"] == "faq" for p in parts):
        parts.append({"id": "faq", "kind": "faq", "heading": "FAQ",
                      "markdown": "## FAQ\n\n" + "\n\n".join(f"### {f['question']}\n{f['answer']}" for f in faq)})
    md = _assemble(plan["title"], parts)
    cited = [s for s in research.get("sources", []) if s["url"] in md]
    if cited:
        parts.append({"id": "sources", "kind": "sources", "heading": "Sources",
                      "markdown": "## Sources\n\n" + "\n".join(f"- <{s['url']}>" for s in cited)})
        md = _assemble(plan["title"], parts)
    meta_desc = (plan.get("meta_description") or "")[:160]
    return {
        "topic": req["topic"], "title": plan["title"], "article_type": article_type,
        "meta_title": (plan.get("meta_title") or plan["title"])[:60], "meta_description": meta_desc,
        "slug": _slugify(plan.get("slug") or plan["title"]),
        "primary_keyword": primary, "secondary_keywords": secondary,
        "outline": [s["heading"] for s in plan["sections"]],
        "parts": parts, "markdown": md, "faq": faq,
        "json_ld": build_json_ld(plan["title"], meta_desc, brand, req["url"], faq),
        "keyword_report": keyword_report(md, plan["title"], primary, secondary),
        "geo_check": quick_geo_check(md, faq),
        "facts_to_verify": sorted(set(FACT_PLACEHOLDER_RE.findall(md))),
        "sources": cited, "brand_facts": facts, "research_used": bool(research.get("used")),
        "mode": mode,
        "distribution_plan": [
            f"Publish at {req['url'].rstrip('/')}/blog/{_slugify(plan.get('slug') or plan['title'])} with the JSON-LD in the page head",
            "Share a summary on LinkedIn and in relevant community threads, disclosing your affiliation",
            "Turn the FAQ into a short video or carousel",
            "Re-run the analysis for this query in 3-4 weeks to measure the change",
        ],
    }


def generate_blog_events(req: Dict, gen: Optional[TextGenerator] = None) -> Iterator[Dict]:
    """Yields progress events; the last one is {"type": "done", "draft": {...}}."""
    gen = gen or TextGenerator()
    brand = brand_from_url(req["url"])
    keywords = [k for k in req.get("keywords", []) if k.strip()]
    primary, secondary = (keywords[0] if keywords else req["topic"]), keywords[1:]
    competitors = [brand_from_url(c) for c in req.get("competitors", []) if c.strip()]
    article_type = req.get("article_type") or "auto"
    if article_type not in ARTICLE_TYPES:
        article_type = pick_article_type(req["topic"])
    yield {"type": "status", "step": "type", "message": f"Article type: {ARTICLE_TYPES[article_type]['label']}"}

    yield {"type": "status", "step": "brand", "message": f"Reading {urlparse(req['url']).netloc or req['url']}"}
    facts = gather_brand_context(req["url"], gen)
    yield {"type": "brand", "data": facts}

    research = {"notes": "", "sources": [], "used": False}
    if req.get("use_research", True):
        yield {"type": "status", "step": "research", "message": "Researching the topic on the web"}
        research = research_topic(req["topic"], keywords, req.get("region", ""), req.get("industry", ""))
        yield {"type": "research", "data": {"used": research.get("used", False), "sources": research.get("sources", []),
                                            "error": research.get("error")}}

    if not gen.available:
        yield {"type": "status", "step": "plan", "message": "No writer API key: using the offline template"}
        plan, md = _template_blog(req["topic"], brand, primary, secondary, competitors)
        chunks = re.split(r"\n(?=## )", md.split("\n", 2)[2])
        parts = [{"id": "intro" if i == 0 else f"s{i}", "kind": "intro" if i == 0 else "section",
                  "heading": "" if i == 0 else c.splitlines()[0].lstrip("# "), "markdown": c.strip()}
                 for i, c in enumerate(chunks) if not c.startswith("## FAQ")]
        plan["sections"] = [{"heading": h} for h in plan["outline"]]
        yield {"type": "plan", "data": {"title": plan["title"], "article_type": article_type,
                                        "sections": [p["heading"] or "Introduction" for p in parts]}}
        for i, p in enumerate(parts):
            yield {"type": "part", "index": i, "part": p}
        yield {"type": "done", "draft": _finish(req, plan, parts, facts, research, "template", article_type)}
        return

    ctx = _context_block(brand, req["url"], facts, research, competitors)
    yield {"type": "status", "step": "plan", "message": "Planning the outline"}
    plan = plan_article(gen, req["topic"], article_type, brand, primary, secondary, req.get("audience", ""),
                        req.get("tone", "helpful and expert"), int(req.get("target_words", 1500)), ctx)
    jobs = ([("intro", None)] + [("section", s) for s in plan["sections"]] + [("conclusion", None)])
    yield {"type": "plan", "data": {"title": plan["title"], "article_type": article_type,
                                    "sections": ["Introduction"] + [s["heading"] for s in plan["sections"]] + ["Conclusion"]}}

    args = (plan, article_type, primary, secondary, req.get("tone", "helpful and expert"), req.get("audience", ""), ctx)
    parts: List[Dict] = []
    with ThreadPoolExecutor(max_workers=int(os.getenv("WRITER_WORKERS", "4"))) as pool:
        futures = [pool.submit(_write, gen, kind, *args, section=sec) for kind, sec in jobs]
        for i, ((kind, sec), fut) in enumerate(zip(jobs, futures)):
            yield {"type": "status", "step": "write", "message": f"Writing part {i + 1} of {len(jobs)}"}
            try:
                text = fut.result()
            except Exception as e:
                heading = sec["heading"] if sec else kind.title()
                text = f"## {heading}\n\n[SOURCE: this section failed to generate ({e}); rewrite it]"
            part = {"id": kind if kind != "section" else f"s{i}", "kind": kind,
                    "heading": sec["heading"] if sec else ("" if kind == "intro" else "Conclusion"), "markdown": text}
            parts.append(part)
            yield {"type": "part", "index": i, "part": part}

    yield {"type": "status", "step": "check", "message": "Checking keywords, schema and facts"}
    yield {"type": "done", "draft": _finish(req, plan, parts, facts, research, f"llm:{gen.provider}", article_type)}


def write_blog_v2(req: Dict, gen: Optional[TextGenerator] = None) -> Dict:
    draft = None
    for ev in generate_blog_events(req, gen):
        if ev["type"] == "done":
            draft = ev["draft"]
    return draft
