"""
Paper metadata fetcher for arXiv and Crossref (DOI).
Provides automated extraction of title, authors, year, abstract, BibTeX, and PDF download.
"""

import re
import urllib.parse
import xml.etree.ElementTree as ET
import requests

HEADERS = {
    "User-Agent": "MicroBench-Academic-Assistant/1.0 (mailto:academic_workbench@xjtu.edu.cn)"
}

PDF_DOWNLOAD_TIMEOUT = 30  # seconds
PDF_MAX_SIZE_MB = 50       # hard cap to avoid filling disk

def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()

# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """
    Fetch paper metadata from arXiv official API.
    Supports formats: '2303.15982', 'arXiv:2303.15982v1', or full url.

    Uses HTTPS by default; falls back to Semantic Scholar if arXiv API is
    unreachable (some corporate/academic networks block port 80).
    """
    clean_id = _normalize_arxiv_id(arxiv_id)
    # Prefer HTTPS — some networks block export.arxiv.org:80 (HTTP)
    api_urls = [
        f"https://export.arxiv.org/api/query?id_list={clean_id}",
        f"http://export.arxiv.org/api/query?id_list={clean_id}",
    ]
    last_err = None
    for api_url in api_urls:
        try:
            resp = requests.get(api_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            break
        except (requests.RequestException, requests.Timeout) as e:
            last_err = e
            continue
    else:
        # Both attempts failed → try Semantic Scholar as a last-resort fallback
        try:
            return _fetch_arxiv_fallback_semanticscholar(clean_id)
        except Exception as s2_err:
            # Re-raise original arXiv error with clear context
            raise RuntimeError(
                f"arXiv API 不可达 (HTTPS + HTTP 均失败: {last_err}). "
                f"Semantic Scholar 兜底失败: {s2_err}"
            ) from last_err

    root = ET.fromstring(resp.text)

    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv 未找到该文献: {clean_id}")

    title_elem = entry.find("atom:title", ns)
    title = clean_text(title_elem.text) if title_elem is not None else "Unknown Title"

    summary_elem = entry.find("atom:summary", ns)
    abstract = clean_text(summary_elem.text) if summary_elem is not None else ""

    published_elem = entry.find("atom:published", ns)
    year = published_elem.text[:4] if published_elem is not None and published_elem.text else "2024"

    authors = []
    for author_elem in entry.findall("atom:author", ns):
        name_elem = author_elem.find("atom:name", ns)
        if name_elem is not None and name_elem.text:
            authors.append(clean_text(name_elem.text))

    doi_elem = entry.find("arxiv:doi", ns)
    doi = doi_elem.text.strip() if doi_elem is not None and doi_elem.text else ""

    primary_cat = entry.find("arxiv:primary_category", ns)
    category = primary_cat.attrib.get("term", "") if primary_cat is not None else ""

    return {
        "title": title,
        "authors": ", ".join(authors),
        "year": int(year) if year.isdigit() else 2024,
        "venue": f"arXiv [{category}]" if category else "arXiv",
        "doi": doi,
        "arxiv": clean_id,
        "abstract": abstract,
        "source": "arXiv",
    }

def _normalize_arxiv_id(arxiv_id: str) -> str:
    """Extract clean arXiv id like '2303.15982' (with optional v1 stripped for PDF URL)."""
    match = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", arxiv_id)
    if not match:
        raise ValueError(f"无法识别的 arXiv ID 格式: {arxiv_id}")
    return match.group(1)

def download_arxiv_pdf(arxiv_id: str, save_path) -> dict:
    """
    Download PDF from arXiv directly.
    save_path: target Path or str. Caller must ensure parent dir exists & path is safe.
    Returns dict with status/size.
    """
    clean_id = _normalize_arxiv_id(arxiv_id)
    pdf_url = f"https://arxiv.org/pdf/{clean_id}"

    resp = requests.get(
        pdf_url,
        headers=HEADERS,
        timeout=PDF_DOWNLOAD_TIMEOUT,
        stream=True,
        allow_redirects=True,
    )
    resp.raise_for_status()

    # Content-Type sanity check
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and "octet-stream" not in content_type.lower():
        raise ValueError(f"arXiv 返回非 PDF 内容: {content_type}")

    # Stream to disk with size cap
    save_path = str(save_path)
    written_bytes = 0
    cap_bytes = PDF_MAX_SIZE_MB * 1024 * 1024
    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            written_bytes += len(chunk)
            if written_bytes > cap_bytes:
                f.close()
                try:
                    import os
                    os.remove(save_path)
                except OSError:
                    pass
                raise ValueError(f"PDF 超过 {PDF_MAX_SIZE_MB}MB 上限, 已中止")
            f.write(chunk)

    return {
        "status": "success",
        "bytes": written_bytes,
        "url": pdf_url,
        "source": "arXiv",
    }

# ---------------------------------------------------------------------------
# Crossref (DOI)
# ---------------------------------------------------------------------------

def fetch_crossref_doi_metadata(doi: str) -> dict:
    """
    Fetch paper metadata from Crossref API using DOI.

    IEEE papers often have empty abstracts in Crossref (copyright gating),
    so as a fallback we scrape the publisher's landing page for the abstract.
    """
    clean_doi = _normalize_doi(doi)
    url = f"https://api.crossref.org/works/{urllib.parse.quote(clean_doi)}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    item = data.get("message", {})

    title_list = item.get("title", [])
    title = title_list[0] if title_list else "Unknown Title"

    authors = []
    for a in item.get("author", []):
        given = a.get("given", "")
        family = a.get("family", "")
        authors.append(f"{given} {family}".strip())

    published = item.get("published-print") or item.get("published-online") or item.get("created")
    year = 2024
    if published and "date-parts" in published and published["date-parts"]:
        year = published["date-parts"][0][0]

    venue_list = item.get("container-title", [])
    venue = venue_list[0] if venue_list else item.get("publisher", "IEEE/ACM")

    abstract = item.get("abstract", "")
    abstract = re.sub(r"<[^>]+>", "", abstract)
    abstract = clean_text(abstract)

    # Fallback chain when Crossref returns empty abstract (common for IEEE):
    #   1) Semantic Scholar Graph API (free, no auth, has most abstracts)
    #   2) IEEE landing page scrape (often blocked by AWS WAF but worth trying)
    abstract_source = "Crossref"
    if not abstract:
        s2_abstract = _fetch_semantic_scholar_abstract(clean_doi)
        if s2_abstract:
            abstract = s2_abstract
            abstract_source = "Crossref+SemanticScholar"
        else:
            try:
                landing = resolve_doi_landing_url(clean_doi)
                scraped = _scrape_ieee_abstract(landing)
                if scraped:
                    abstract = scraped
                    abstract_source = "Crossref+LandingPage"
            except Exception:
                pass

    return {
        "title": clean_text(title),
        "authors": ", ".join(authors) if authors else "Unknown",
        "year": year,
        "venue": clean_text(venue),
        "doi": clean_doi,
        "arxiv": "",
        "abstract": abstract,
        "source": abstract_source,
    }

def _normalize_doi(doi: str) -> str:
    doi_match = re.search(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", doi)
    if not doi_match:
        raise ValueError(f"无法识别的 DOI 格式: {doi}")
    return doi_match.group(1).rstrip(".")


def _fetch_semantic_scholar_abstract(doi: str) -> str:
    """
    Fallback abstract source: Semantic Scholar Graph API.
    Free, no auth required, has abstracts for most major publisher papers
    (including IEEE, ACM, Elsevier) that Crossref often withholds.

    Rate limit: ~100 req / 5 min without API key. We only call this when
    Crossref gives us nothing, so impact is minimal.
    """
    try:
        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
        resp = requests.get(
            url,
            headers=HEADERS,
            params={"fields": "title,abstract,year,venue,authors"},
            timeout=20,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        return clean_text(data.get("abstract") or "")
    except (requests.RequestException, ValueError, KeyError):
        return ""


def _fetch_arxiv_fallback_semanticscholar(arxiv_id: str) -> dict:
    """
    Fallback for arXiv metadata fetch: use Semantic Scholar's arXiv lookup
    endpoint when export.arxiv.org is unreachable (e.g. corporate networks
    blocking port 80).

    Returns the same dict shape as fetch_arxiv_metadata so callers don't
    need to special-case the source.
    """
    # Semantic Scholar accepts arXiv IDs via the ARXIV: prefix
    url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{arxiv_id}"
    resp = requests.get(
        url,
        headers=HEADERS,
        params={"fields": "title,abstract,year,venue,authors,externalIds"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Semantic Scholar arXiv 兜底失败: HTTP {resp.status_code}"
        )
    data = resp.json()
    authors_list = data.get("authors") or []
    authors = ", ".join(
        a.get("name", "").strip() for a in authors_list if a.get("name")
    )
    venue = data.get("venue") or ""
    return {
        "title": clean_text(data.get("title") or ""),
        "authors": authors,
        "year": int(data.get("year") or 2024),
        "venue": venue if venue else f"arXiv (Semantic Scholar 兜底)",
        "doi": (data.get("externalIds") or {}).get("DOI", ""),
        "arxiv": arxiv_id,
        "abstract": clean_text(data.get("abstract") or ""),
        "source": "arXiv (via Semantic Scholar)",
    }


def _scrape_ieee_abstract(landing_url: str) -> str:
    """
    Best-effort: scrape the IEEE Xplore landing page for the abstract.
    Crossref doesn't return abstracts for IEEE papers, but IEEE's HTML
    landing page exposes them via <meta name="description"> or JSON-LD.
    NOTE: IEEE Xplore uses AWS WAF bot detection which blocks most automated
    requests, so this rarely succeeds — Semantic Scholar is preferred.
    Returns empty string if nothing found.
    """
    try:
        resp = requests.get(
            landing_url,
            headers={**HEADERS, "Accept": "text/html"},
            timeout=20,
            allow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text

        # Skip if it's just an AWS WAF challenge page
        if "awswaf" in html.lower() or len(html) < 5000:
            return ""

        # 1) <meta name="description">
        m = re.search(
            r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if m:
            text = re.sub(r"<[^>]+>", "", m.group(1))
            return clean_text(text)

        # 2) <meta property="og:description">
        m = re.search(
            r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if m:
            text = re.sub(r"<[^>]+>", "", m.group(1))
            return clean_text(text)

        # 3) JSON-LD structured data
        for m in re.finditer(
            r'<script\s+type=["\']application/ld\+json["\']>(.*?)</script>',
            html,
            re.DOTALL,
        ):
            try:
                import json
                ld = json.loads(m.group(1))
                if isinstance(ld, dict) and "abstract" in ld:
                    return clean_text(ld["abstract"])
            except (ValueError, KeyError):
                pass
    except requests.RequestException:
        pass
    return ""


def resolve_doi_landing_url(doi: str) -> str:
    """
    Resolve DOI to publisher landing page URL via doi.org redirect.
    Returns the final landing URL (not the PDF directly — most publishers
    require browser JS / login to fetch PDF, so we only expose the link).
    """
    clean_doi = _normalize_doi(doi)
    resp = requests.head(
        f"https://doi.org/{clean_doi}",
        headers=HEADERS,
        timeout=15,
        allow_redirects=True,
    )
    resp.raise_for_status()
    return resp.url

# ---------------------------------------------------------------------------
# BibTeX generation
# ---------------------------------------------------------------------------

def generate_bibtex(meta: dict, bib_key: str = None) -> str:
    """
    Build a BibTeX entry from fetched metadata.
    bib_key: optional explicit key; otherwise auto-generate from first author + year + first title word.
    """
    title = meta.get("title", "Untitled").strip()
    authors_raw = meta.get("authors", "")
    year = meta.get("year", "")
    venue = meta.get("venue", "")
    doi = meta.get("doi", "")
    arxiv = meta.get("arxiv", "")
    source = meta.get("source", "")

    # Parse authors into BibTeX "and"-joined list
    if authors_raw and authors_raw != "Unknown":
        author_list = [a.strip() for a in re.split(r",\s*|;\s*", authors_raw) if a.strip()]
        authors_field = " and ".join(author_list)
    else:
        authors_field = "Unknown"

    # Determine entry type
    if source == "arXiv" and not doi:
        entry_type = "misc"
        # arXiv preprint
        fields = {
            "author": authors_field,
            "title": title,
            "year": str(year),
            "eprint": arxiv,
            "archivePrefix": "arXiv",
            "primaryClass": "",
        }
    else:
        # Treat as article; user can rename later
        entry_type = "article"
        fields = {
            "author": authors_field,
            "title": title,
            "journal": venue,
            "year": str(year),
            "doi": doi,
        }
    if arxiv and entry_type == "article":
        fields["note"] = f"arXiv:{arxiv}"

    # Build key
    if not bib_key:
        first_author = authors_field.split(" and ")[0].split(",")[0].strip().lower()
        first_author = re.sub(r"[^a-z]", "", first_author) or "anon"
        first_title_word = re.sub(r"[^A-Za-z]", "", title.split()[0] if title.split() else "untitled")
        bib_key = f"{first_author}{year}{first_title_word}".lower()

    lines = [f"@{entry_type}{{{bib_key},"]
    for k, v in fields.items():
        if v:
            # Escape BibTeX special chars in title
            if k == "title":
                v = v.replace("{", "\\{").replace("}", "\\}")
            lines.append(f"  {k} = {{{v}}},")
    lines.append("}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Smart dispatch
# ---------------------------------------------------------------------------

def smart_fetch_paper(identifier: str) -> dict:
    """
    Intelligently dispatch to arXiv or Crossref based on input string.
    """
    identifier = identifier.strip()
    if "arxiv" in identifier.lower() or re.search(r"^\d{4}\.\d{4,5}", identifier):
        return fetch_arxiv_metadata(identifier)
    elif "10." in identifier or "doi.org" in identifier:
        return fetch_crossref_doi_metadata(identifier)
    else:
        # Try arXiv first, if fails try DOI
        try:
            return fetch_arxiv_metadata(identifier)
        except Exception:
            return fetch_crossref_doi_metadata(identifier)