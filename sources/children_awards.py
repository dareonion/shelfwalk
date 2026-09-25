"""Official children's award archives. Keep honors, shortlists and winners distinct.

Parsers consume saved bytes; network and export policy lives in children.py.
No age suitability is inferred from an award alone.
"""
from __future__ import annotations

import io
import re

from bs4 import BeautifulSoup
from pypdf import PdfReader


def clean(value):
    value = re.sub(r"\s+", " ", value or "").strip()
    return re.sub(r"\s+([,.;!?])", r"\1", value)


def credit(raw):
    """Extract bibliographic facts; preserve the complete source credit as well.

    Illustration prizes name the artist first: never silently call that person
    the author when the source explicitly distinguishes their roles.
    """
    raw = clean(raw).replace("w ritten", "written").replace("Origina l", "Original")
    raw = re.sub(r"\bBy,\s+", "by ", raw)
    raw = re.sub(r"\billustrated (?=[A-Z])", "illustrated by ", raw)
    markers = list(re.finditer(
        r"(?:,?\s*)(?:(?:written|retold|adapted|compiled|selected|translated|illustrated|illus\.|illustrating)"
        r"(?:\s+(?:and|&)\s+(?:written|illustrated|adapted))?\s+(?:with photographs\s+)?){0,1}by\s+", raw, re.I))
    explicit = [m for m in markers if re.search(r"written|retold|adapted|compiled|selected|illustrat|illus\.", m[0], re.I)]
    cutoff = re.search(r",?\s+(?:and )?translated (?:from|by)", raw, re.I)
    bare = [m for m in markers if not cutoff or m.start() < cutoff.start()]
    marker = explicit[0] if explicit else bare[-1] if bare else None
    if explicit and not re.search(r"written|retold|adapted|selected|compiled|translated", explicit[0][0], re.I):
        preceding = [m for m in bare if m.start() < explicit[0].start()]
        if preceding:
            marker = preceding[-1]
    if not marker:
        # Older Belpré bibliographies use "Title. Author. (Publisher)".
        old = re.match(r"^(.+?)\. ([^.]+)\. \(", raw)
        if old:
            return {"title": old[1], "author": old[2], "illustrator": None, "credit": raw}
        return {"title": raw, "author": None, "illustrator": None, "credit": raw}
    title = raw[:marker.start()].strip(" ,.;:\"“”")
    tail = raw[marker.start():]
    name_end = r"(?=\s*[,;]\s*(?:written|illustrated|illus\.|text(?:\s+by|:)|translated|and published|is the|photos by)|\s+and\s+(?:illustrated|published|translated)|\s*\(|\s+Original\s+text|;\s*music:|$)"
    author = re.search(r"(?:written|retold|adapted|text(?:\s+by|:))\s*(?:by\s+)?(.+?)" + name_end, tail, re.I)
    illustrator = re.search(r"(?:illustrated|illustrating|illus\.)\s+by\s+(.+?)" + name_end, tail, re.I)
    both = re.search(r"(?:written|retold|adapted|compiled|selected|illustrated)\s+(?:and|&)\s+(?:written|illustrated)\s+(?:with photographs\s+)?by\s+(.+?)" + name_end, tail, re.I)
    if both:
        author = illustrator = both
    elif not author and not re.search(r"illustrat|illus\.", marker.group(), re.I):
        author = re.search(r"by\s+(.+?)" + name_end, tail, re.I)
    return {"title": title,
            "author": clean(author.group(1)).strip(" ,.;") if author else None,
            "illustrator": clean(illustrator.group(1)).strip(" ,.;") if illustrator else None,
            "credit": raw}


def pdf_text(raw):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(raw)).pages)


def parse_ala_pdf(raw, source):
    """ALSC's six full-history bibliographies, including wrapped PDF entries."""
    out, pending = [], []
    year = status = None
    category = {"caldecott": "Illustration", "geisel": "Beginning readers",
                "newbery": "Children's literature", "sibert": "Informational",
                "belpre": "Narrative", "batchelder": "English translation"}[source]

    def flush():
        if pending and year and status:
            raw_credit = clean(" ".join(pending))
            if not re.search(r"none recorded|no award|not awarded", raw_credit, re.I):
                if source == "batchelder":
                    raw_credit = re.sub(r"^.*?,\s*for\s+", "", raw_credit, flags=re.I)
                row = credit(raw_credit)
                if row["title"]:
                    out.append(dict(row, year=year, category=category, status=status))
        pending.clear()

    for line in pdf_text(raw).splitlines():
        line = clean(line)
        line = line.replace("Joseph Had a Little Overcoat Simms Taback", "Joseph Had a Little Overcoat by Simms Taback")
        line = line.replace("Mr. Lepron’s Mystery Soup, Giovanna Zoboli", "Mr. Lepron’s Mystery Soup, written by Giovanna Zoboli")
        if re.fullmatch(r"\([^)]+\)\.?", line) and not pending and out:
            out[-1]["credit"] += " " + line
            continue
        if not line or re.match(r"^(?:©?\s*Association for Library|American Library Association|www\.ala\.org)", line):
            continue
        ym = re.match(r"^(19\d\d|20\d\d)(?:\s+(.*))?$", line)
        if ym:
            flush()
            year = int(ym[1])
            status = "winner"
            line = ym[2] or ""
            if not line:
                continue
        if not year:
            continue
        if re.match(r"^For (?:children|youth|young adult|narrative|illustration)", line, re.I):
            flush()
            category = re.sub(r"^For\s+", "", line, flags=re.I)
            status = "winner"
            continue
        heading = re.match(r"^(?:(?:Medal\s+)?Winner[s]?|Honor(?:s|\s+Books?)?)(?:\s+for[^:]*)?\s*:?\s*", line, re.I)
        if heading:
            flush()
            status = "honor" if line.lower().startswith("honor") else "winner"
            line = line[heading.end():]
            if not line:
                continue
        if re.match(r"^(?:Two awards given|No honor books|Also published|\[?None recorded|\{None recorded|No award|Not awarded)", line, re.I):
            continue
        # Batchelder entries often lack a final publisher parenthesis.
        if source == "batchelder" and pending and re.search(r",\s*for\s+", line):
            flush()
        pending.append(line)
        joined = " ".join(pending)
        if source != "batchelder" and (
            (re.search(r"\)\.?$", line) and joined.count("(") == joined.count(")"))
            or ("published by" in joined and line.endswith("."))
        ):
            flush()
    flush()
    return out


def parse_ejk(raw):
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    for prefix, selector, status in (("winner", ".award_bottom_container", "winner"),
                                     ("honor", ".honor_bottom_container", "honor")):
        for block in soup.select(selector):
            heading = block.select_one(f".{prefix}_year_and_award")
            title = block.select_one(f".{prefix}_book_title")
            if not heading or not title:
                continue
            h = heading.get_text(" ", strip=True)
            ym = re.search(r"\b(19\d\d|20\d\d)\b", h)
            if not ym:
                continue
            category = "Illustrator" if "Illustrator" in h else "Writer"
            creator = heading.parent.find_next_sibling("a") if heading.parent.name == "a" else heading.find_next_sibling("a")
            # Some older credits are plain text rather than links.
            chunks = list(block.stripped_strings)
            name = creator.get_text(" ", strip=True) if creator else chunks[1]
            co = block.select_one(f".{prefix}_co_creator")
            cotext = co.get_text(" ", strip=True) if co else ""
            author = name if category == "Writer" else re.sub(r"^Written\s+(?:by\s+)?", "", cotext, flags=re.I) or None
            illustrator = name if category == "Illustrator" else re.sub(r"^Illustrated\s+(?:by\s+)?", "", cotext, flags=re.I) or None
            out.append(dict(title=clean(title.get_text(" ", strip=True)), author=author,
                            illustrator=illustrator, year=int(ym[1]), category=category,
                            status=status, credit=clean(block.get_text(" ", strip=True))))
    return out


def parse_carnegie_winners(raw, illustration=False):
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    content = soup.select_one(".entry-content") or soup
    # Individual year entries are separated with <br>, with titles emphasized.
    for paragraph in content.select("p"):
        for fragment in re.split(r"<br\s*/?>", str(paragraph)):
            block = BeautifulSoup(fragment, "html.parser")
            text = clean(block.get_text(" ", strip=True))
            match = re.match(r"^(19\d\d|20\d\d)\s+(.+?),\s*(.+)", text)
            if not match:
                continue
            emph = [clean(e.get_text(" ", strip=True)).strip(" ,") for e in block.select("em")]
            title = " ".join(emph).strip(" ,") if emph else match[3].rsplit(",", 1)[0].strip()
            if not title:
                continue
            year = int(match[1])
            out.append(dict(title=title, author=None if illustration else match[2],
                            illustrator=match[2] if illustration else None,
                            year=year, year_basis="publication" if year < 2007 else "award",
                            category="Illustration" if illustration else "Writing",
                            status="winner", credit=text))
    return out


def parse_carnegie_shortlists(raw):
    soup = BeautifulSoup(raw, "html.parser")
    content = soup.select_one(".entry-content") or soup
    out = []
    h1 = soup.find("h1")
    default = re.search(r"20\d\d", h1.get_text()) if h1 else None
    year = int(default[0]) if default else None
    category = None
    status = "shortlist"
    elements = []
    for el in content.find_all(["h2", "h3", "h4", "p", "li"]):
        if el.find_parent("li"):
            continue
        for fragment in re.split(r"<br\s*/?>", str(el)):
            elements.append(BeautifulSoup(fragment, "html.parser"))
    for el in elements:
        text = clean(el.get_text(" ", strip=True))
        heading = re.search(r"(?:Carnegie|Kate Greenaway).*?(?:Medal|Shortlist|Longlist)", text, re.I)
        if heading and len(text) < 180 and " by " not in text.replace("by author surname", "").replace("by illustrator surname", "") and "download" not in text.lower():
            ym = re.search(r"20\d\d", text)
            year = int(ym[0]) if ym else year
            status = "longlist" if "longlist" in text.lower() else "shortlist"
            category = "Illustration" if re.search(r"Greenaway|Illustration", text, re.I) else "Writing"
            continue
        if not year or not category or re.search(r"download|resources", text, re.I):
            continue
        row = credit(text)
        title = el.find("em")
        if title and title.get_text(strip=True):
            row["title"] = clean(title.get_text(" ", strip=True)).strip(" ,")
        if row["title"] == text:
            continue
        if category == "Illustration":
            # Award entries list the artist first, sometimes without a by-line.
            tail = text[len(row["title"]):].lstrip(" ,")
            first = re.sub(r"^(?:(?:written|illustrated|adapted)(?: (?:and|&) (?:written|illustrated|adapted))? )?by +", "", tail, flags=re.I)
            row["illustrator"] = re.split(r"\(|, (?:written|translated)| and written", first)[0].strip(" ,") or None
            author = re.search(r"written by\s+(.+?)(?=,\s*(?:translated|illustrated)|\s*\(|\)|$)", tail, re.I)
            explicit = re.search(r"and ([^()]+)\s*\(authors?\)", tail, re.I)
            both = re.search(r"(?:written and illustrated|illustrated and written)", tail, re.I)
            row["author"] = (clean(author[1]) if author else clean(explicit[1]) if explicit else row["illustrator"] if both else None)
        out.append(dict(row, year=year, category=category, status=status, year_basis="award"))
    return out


def parse_csk_table(raw):
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    for tr in soup.select("table tbody tr"):
        title = tr.select_one("td.views-field-title-1 a")
        rank = tr.select_one("td.views-field-field-winner-rank")
        if not title or not rank:
            continue
        m = re.search(r"(\d{4})\s*-\s*(Winner|Honor)", rank.get_text(), re.I)
        if m:
            out.append(dict(title=clean(title.get_text()), author=None, illustrator=None,
                            year=int(m[1]), status=m[2].lower(), category="Unspecified in archive table",
                            credit=None, record_url="https://www.ala.org" + title["href"]))
    return out


def parse_board_award(raw):
    soup = BeautifulSoup(raw, "html.parser")
    content = soup.select_one(".intro-content") or soup
    out = []
    year = status = None
    for el in content.find_all(["h3", "p", "li"]):
        text = clean(el.get_text(" ", strip=True))
        m = re.fullmatch(r"(20\d\d) Award Winners", text)
        if m:
            year = int(m[1])
        elif text in ("Gold Medals", "Gold Medal", "Silver Medal", "Silver Medals"):
            status = "winner" if text.startswith("Gold") else "honor"
        elif el.name == "h3":
            year = status = None
        elif year and status and el.name == "li":
            out.append(dict(credit(text), year=year, status=status, category="Board books, ages 0–3"))
    return out


def parse_csk_year(raw, year):
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    category = status = None
    content = soup.find("article") or soup
    for el in content.find_all(["h2", "p"]):
        text = clean(el.get_text(" ", strip=True))
        if el.name == "h2":
            if not re.search(r"Author|Illustrator", text) or "Lifetime" in text:
                category = status = None
                continue
            category = ("John Steptoe " if "Steptoe" in text else "") + ("Illustrator" if "Illustrator" in text else "Author")
            status = "honor" if "Honor" in text else "winner"
            continue
        if not category or not re.search(r"\b(?:written|illustrated)\b", text, re.I):
            continue
        row = credit(text)
        quoted = re.match(r'^["“](.+?)["”]', text)
        if quoted:
            row["title"] = quoted[1].rstrip(" ,")
        for field, pattern in (("author", r"written by (.+?)(?= and published|, illustrated|, and published|$)"),
                               ("illustrator", r"illustrated by (.+?)(?=, written|, is the| and published|$)")):
            match = re.search(pattern, text)
            if not row.get(field) and match:
                row[field] = clean(match[1])
        out.append(dict(row, year=year, category=category, status=status))
    return out


def parse_hornbook(raw):
    soup = BeautifulSoup(raw, "html.parser")
    content = soup.select_one(".story-para")
    if not content:
        return []
    out = []
    year = category = None
    for el in content.find_all(["h3", "h4", "p"]):
        text = clean(el.get_text(" ", strip=True))
        if re.fullmatch(r"(?:19|20)\d\d", text):
            year = int(text)
            continue
        if el.name == "h4" and text:
            category = text
            continue
        if not year or not category or el.name != "p" or not el.find("em"):
            continue
        status = "honor" if "Honor" in text[:60] else "winner"
        text = re.sub(r"^.*?Honor Books?\s*:\s*", "", text)
        for chunk in re.split(r"(?<=\));\s*", text):
            # Old years use 'Title, by Author' consistently; embedded '; illustrated'
            # remains part of the same book's credit.
            if not re.search(r"\bby\b", chunk):
                continue
            out.append(dict(credit(chunk), year=year, category=category, status=status))
    return out


def parse_zolotow(raw):
    soup = BeautifulSoup(raw, "html.parser")
    out = []
    year = status = None
    year_label = None
    for el in soup.find_all(["h2", "h3", "div"]):
        if el.name == "h2":
            text = clean(el.get_text())
            if re.fullmatch(r"(?:19|20)\d\d(?:-(?:19|20)\d\d)?", text):
                year_label = text
                year = int(text[-4:])
        elif el.name == "h3":
            # Source markup leaves h3 open around books; read only heading text.
            text = clean(" ".join(el.find_all(string=True, recursive=False)))
            if "Commended" in text:
                status = "commended"
            elif "Honor" in text:
                status = "honor"
            elif "Winner" in text:
                status = "winner"
        elif "bookInfoColumn" in el.get("class", []) and year and status:
            link = el.find("a")
            chunks = list(el.stripped_strings)
            if not link or len(chunks) < 2:
                continue
            details = clean(" ".join(chunks[2:]))
            age = re.search(r"(?:Ages?\s*)?(\d+)\s*[-–]\s*(\d+)\s*$", details)
            illustrator = re.search(r"Illustrated by (.+?)\.", details)
            out.append(dict(title=clean(link.get_text()),
                            author=re.sub(r"^by\s+", "", clean(chunks[1])),
                            illustrator=illustrator[1] if illustrator else None,
                            year=year, year_label=year_label, category="Picture-book text", status=status,
                            source_min_age=int(age[1]) if age else None,
                            source_max_age=int(age[2]) if age else None,
                            credit=clean(" ".join(chunks))))
    return out
