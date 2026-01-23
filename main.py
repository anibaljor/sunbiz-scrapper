from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
import uvicorn
import re
import time
from typing import Dict, Optional, List
from playwright.sync_api import sync_playwright
from html import unescape

app = FastAPI(title="Sunbiz Scraper API")


def extract_block(text: str, heading: str, stop_headings=None) -> Optional[str]:
    if stop_headings is None:
        stop_headings = [
            "Mailing Address",
            "Registered Agent Name & Address",
            "Authorized Person",
            "Authorized Person(s) Detail",
            "Filing Information",
            "Principal Address",
        ]
    idx = text.find(heading)
    if idx == -1:
        return None
    rest = text[idx + len(heading) :]
    lines = [ln.strip() for ln in rest.splitlines()]
    out_lines = []
    for ln in lines:
        if not ln:
            if out_lines:
                break
            else:
                continue
        for sh in stop_headings:
            if ln.startswith(sh):
                return "\n".join(out_lines).strip() if out_lines else None
        out_lines.append(ln)
        if len(out_lines) > 20:
            break
    return "\n".join(out_lines).strip() if out_lines else None


def parse_label_value_block(text: str) -> Dict[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {}
    i = 0
    while i < len(lines):
        label = lines[i]
        value = lines[i + 1] if i + 1 < len(lines) else ""
        out[label] = value
        i += 2
    return out


def _strip_tags_and_unescape(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join([ln for ln in lines if ln])


def parse_authorized_persons_from_html(html_snippet: str) -> List[Dict[str, Optional[str]]]:
    out: List[Dict[str, Optional[str]]] = []
    if not html_snippet:
        return out
    h = html_snippet.replace('&nbsp;', ' ')
    pattern = re.compile(r'(?i)(<span[^>]*>\s*Title\b.*?</span>)(.*?)(?=(<span[^>]*>\s*Title\b)|$)', re.DOTALL)
    for m in pattern.finditer(h):
        title_span = m.group(1) or ''
        after = m.group(2) or ''
        t = re.sub(r'<[^>]+>', '', title_span).strip()
        title = None
        mt = re.search(r'(?i)Title\s*[:\s]*([^\s<]+)', t)
        if mt:
            title = mt.group(1).strip()
        name = None
        parts = re.split(r'(?i)<\s*(?:span|div)[^>]*>', after, maxsplit=1)
        if parts:
            cand = re.sub(r'<[^>]+>', '', parts[0]).strip()
            if cand:
                name = re.sub(r'\s{2,}', ' ', cand)
        addr = None
        mdiv = re.search(r'(?i)<div[^>]*>(.*?)</div>', after, re.DOTALL)
        if mdiv:
            raw_addr = mdiv.group(1)
            addr = _strip_tags_and_unescape(raw_addr)
        out.append({'title': title, 'name': name, 'address': addr})
    return out


def parse_detail_page(text: str, filing_values: Dict[str, str] = None) -> Dict[str, Optional[str]]:
    t = re.sub(r"\r", "", text)
    t = re.sub(r"\n[ \t]+", "\n", t)
    data = {}
    if filing_values:
        label_map = {
            "Document Number": "document_number",
            "FEI/EIN Number": "fei_ein",
            "Date Filed": "date_filed",
            "State": "state",
            "Status": "status",
            "Last Event": "last_event",
            "Event Date Filed": "event_date_filed",
        }
        for lab, key in label_map.items():
            data[key] = filing_values.get(lab)
    else:
        def get_label_value(label: str, text_block: str) -> Optional[str]:
            idx = text_block.find(label)
            if idx == -1:
                return None
            rest = text_block[idx + len(label) :]
            for line in rest.splitlines():
                ln = line.strip()
                if ln:
                    return ln
            return None
        data["document_number"] = get_label_value("Document Number", t)
        data["fei_ein"] = get_label_value("FEI/EIN Number", t)
        data["date_filed"] = get_label_value("Date Filed", t)
        data["state"] = get_label_value("State", t)
        data["status"] = get_label_value("Status", t)
        data["last_event"] = get_label_value("Last Event", t)
        data["event_date_filed"] = get_label_value("Event Date Filed", t)

    data["principal_address"] = extract_block(t, "Principal Address")
    data["mailing_address"] = extract_block(t, "Mailing Address")
    data["registered_agent"] = extract_block(t, "Registered Agent Name & Address")
    data["authorized_persons"] = extract_block(t, "Authorized Person(s) Detail")
    return data


def scrape_document(doc_number: str, page) -> Dict:
    base = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByDocumentNumber"
    page.goto(base)
    page.fill('input[name="SearchTerm"]', doc_number)
    page.click('input[type="submit"]')
    
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        time.sleep(1)

    content = page.content()
    if "Record Not Found" in content or "Record Not Found" in page.inner_text("body"):
        return {"document_number": doc_number, "found": False}

    text = page.inner_text("body")
    filing_values = None
    try:
        filing_div = page.query_selector("div.detailSection.filingInformation div")
        if filing_div:
            filing_text = filing_div.inner_text()
            filing_values = parse_label_value_block(filing_text)
    except Exception:
        filing_values = None

    parsed = parse_detail_page(text, filing_values=filing_values)
    sections = {}
    sec_elems = page.query_selector_all('div.detailSection')
    for sec in sec_elems:
        try:
            spans = sec.query_selector_all('span')
            header = spans[0].inner_text().strip() if spans else ''
            content_spans = [s.inner_text().strip() for s in spans[1:]]
            div = sec.query_selector('div')
            div_text = div.inner_text().strip() if div else ''
            tables = sec.query_selector_all('table')
            tables_rows = []
            for t in tables:
                rows = []
                trs = t.query_selector_all('tr')
                for idx_tr, tr in enumerate(trs):
                    if idx_tr == 0:
                        ths = tr.query_selector_all('td')
                        if ths and 'Report Year' in ths[0].inner_text():
                            continue
                    tds = tr.query_selector_all('td')
                    if len(tds) >= 2:
                        rows.append({'c1': tds[0].inner_text().strip(), 'c2': tds[1].inner_text().strip()})
                if rows:
                    tables_rows.append(rows)
            sections[header] = {'spans': content_spans, 'div_text': div_text, 'tables': tables_rows, 'inner_html': sec.inner_html()}
        except Exception:
            continue

    pr = sections.get('Principal Address')
    if pr:
        parsed['principal_address'] = pr.get('div_text') or '\n'.join(pr.get('spans', []))
        for s in pr.get('spans', []):
            if 'Changed' in s:
                m = re.search(r'Changed:\s*(\d{2}/\d{2}/\d{4})', s)
                if m:
                    parsed['principal_changed'] = m.group(1)
                    break

    maddr = sections.get('Mailing Address')
    if maddr:
        parsed['mailing_address'] = maddr.get('div_text') or '\n'.join(maddr.get('spans', []))

    ragent = sections.get('Registered Agent Name & Address') or sections.get('Registered Agent Name &amp; Address')
    if ragent:
        name = None
        if ragent.get('spans'):
            name = ragent['spans'][0]
        addr = ragent.get('div_text')
        parsed['registered_agent_name'] = name
        parsed['registered_agent_address'] = addr
        if 'registered_agent' in parsed:
            del parsed['registered_agent']

    authorized = []
    try:
        for sec in page.query_selector_all('div.detailSection'):
            try:
                spans = sec.query_selector_all('span')
                header = spans[0].inner_text().strip() if spans else ''
                if header == 'Authorized Person(s) Detail':
                    authorized = sec.evaluate('''el => {
                        const out = [];
                        const spans = Array.from(el.querySelectorAll('span'));
                        for (let i = 0; i < spans.length; i++) {
                            const s = spans[i];
                            const txt = s.textContent.trim();
                            if (/^Title\b/i.test(txt)) {
                                const title = txt.replace(/^Title\b[:\s]*/i, '').trim() || null;
                                let name = null;
                                let address = null;
                                let node = s.nextSibling;
                                while (node) {
                                    if (node.nodeType === Node.TEXT_NODE) {
                                        const t = node.textContent.trim();
                                        if (t) {
                                            if (!name) name = t;
                                        }
                                    }
                                    if (node.nodeType === Node.ELEMENT_NODE) {
                                        const ne = node;
                                        if (ne.tagName === 'SPAN') {
                                            const d = ne.querySelector('div');
                                            if (d) {
                                                address = d.innerText.replace(/\r/g,'\n').trim();
                                                address = address.replace(/\n\s+/g,'\n');
                                                break;
                                            }
                                            const t2 = ne.textContent.trim();
                                            if (t2 && !/^Title\b/i.test(t2) && !/^Name & Address/i.test(t2)) {
                                                if (!name) name = t2;
                                            }
                                        }
                                        if (ne.tagName === 'DIV') {
                                            const tdiv = ne.innerText.replace(/\r/g,'\n').trim();
                                            if (tdiv) {
                                                address = tdiv.replace(/\n\s+/g,'\n');
                                                break;
                                            }
                                        }
                                    }
                                    node = node.nextSibling;
                                }
                                out.push({title: title, name: name, address: address});
                            }
                        }
                        return out;
                    ''')
                    break
            except Exception:
                continue
    except Exception:
        authorized = []

    if not authorized:
        sec_auth = sections.get('Authorized Person(s) Detail')
        if sec_auth and sec_auth.get('inner_html'):
            try:
                authorized = parse_authorized_persons_from_html(sec_auth.get('inner_html'))
            except Exception:
                authorized = []

    parsed['authorized_persons'] = authorized

    asec = sections.get('Annual Reports')
    annual_reports_table = []
    if asec and asec.get('tables'):
        for rows in asec.get('tables'):
            for r in rows:
                annual_reports_table.append({'year': r.get('c1'), 'filed_date': r.get('c2')})
    parsed['annual_reports_table'] = annual_reports_table

    anchors = page.query_selector_all("a")
    document_images: List[Dict[str, Optional[str]]] = []
    for i, a in enumerate(anchors):
        try:
            txt = a.inner_text().strip()
        except Exception:
            txt = ""
        href = a.get_attribute("href")
        if not txt or not href:
            continue
        up = txt.upper()
        if "-- ANNUAL REPORT" in up or "ANNUAL REPORT" in up or re.match(r"\d{2}/\d{2}/\d{4}", txt):
            pdf_href = None
            for j in range(i + 1, min(i + 6, len(anchors))):
                try:
                    t2 = anchors[j].inner_text().strip()
                except Exception:
                    t2 = ""
                if t2 and "VIEW IMAGE IN PDF FORMAT" in t2.upper():
                    pdf_href = anchors[j].get_attribute("href")
                    break
            document_images.append({"label": txt, "link": href, "pdf": pdf_href})

    base_url = 'https://search.sunbiz.org'
    for it in document_images:
        if it.get('link') and it['link'].startswith('/'):
            it['link'] = base_url + it['link']
        if it.get('pdf') and it['pdf'].startswith('/'):
            it['pdf'] = base_url + it['pdf']

    annual_reports = []
    for item in document_images:
        lab = item.get('label', '')
        if 'ANNUAL REPORT' in lab.upper():
            yr = None
            filed = None
            m = re.search(r"(\d{4})", lab)
            if m:
                yr = m.group(1)
            m2 = re.search(r"(\d{2}/\d{2}/\d{4})", lab)
            if m2:
                filed = m2.group(1)
            annual_reports.append({"label": lab, "year": yr, "filed_date": filed, "link": item.get("link"), "pdf": item.get("pdf")})

    if annual_reports_table:
        parsed['annual_reports'] = annual_reports_table
    else:
        parsed['annual_reports'] = annual_reports

    parsed["document_images"] = document_images
    parsed["document_number_searched"] = doc_number
    parsed["found"] = True
    return parsed


@app.get("/")
async def root():
    return {
        "message": "Sunbiz Scraper API",
        "endpoints": {
            "/scrape": "GET - Scrape document by number (param: doc_number)"
        }
    }


@app.get("/scrape")
async def scrape_endpoint(doc_number: str):
    """
    Scrape Sunbiz document by document number
    Example: /scrape?doc_number=L05000113016
    """
    if not doc_number or len(doc_number.strip()) == 0:
        raise HTTPException(status_code=400, detail="doc_number parameter is required")

    def _sync_scrape(dn: str):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                return scrape_document(dn.strip(), page)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    try:
        result = await run_in_threadpool(_sync_scrape, doc_number)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scraping error: {str(e)}")


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)