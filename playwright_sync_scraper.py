import re
import json
import csv
import time
from typing import Dict, Optional, List
from playwright.sync_api import sync_playwright


DOCUMENT_NUMBERS = [
    "L05000113016",
    "L06000004375",
    "L07000056276",
]


def extract_field(pattern: str, text: str, flags=0) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


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
    # skip leading empty lines
    out_lines = []
    for ln in lines:
        if not ln:
            if out_lines:
                break
            else:
                continue
        # stop if next heading reached
        for sh in stop_headings:
            if ln.startswith(sh):
                return "\n".join(out_lines).strip() if out_lines else None
        out_lines.append(ln)
        # safety limit
        if len(out_lines) > 20:
            break
    return "\n".join(out_lines).strip() if out_lines else None


def parse_label_value_block(text: str) -> Dict[str, str]:
    """Parse a block like:\n    Document Number\n    L05000113016\n    FEI/EIN Number\n    20-...\n    into a dict of label->value."""
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
    from html import unescape
    # replace <br> with newlines, remove other tags, unescape HTML entities
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = unescape(s)
    # normalize whitespace and lines
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join([ln for ln in lines if ln])


def parse_authorized_persons_from_html(html_snippet: str) -> List[Dict[str, Optional[str]]]:
    """Parse repeated Authorized Person entries from the section inner HTML.

    Returns a list of dicts: {title, name, address}.
    """
    out: List[Dict[str, Optional[str]]] = []
    if not html_snippet:
        return out
    h = html_snippet
    # Normalize nbsp
    h = h.replace('&nbsp;', ' ')
    # Find Title blocks and the following content until the next Title
    pattern = re.compile(r'(?i)(<span[^>]*>\s*Title\b.*?</span>)(.*?)(?=(<span[^>]*>\s*Title\b)|$)', re.DOTALL)
    for m in pattern.finditer(h):
        title_span = m.group(1) or ''
        after = m.group(2) or ''
        # extract title text from the title_span
        t = re.sub(r'<[^>]+>', '', title_span)
        t = t.strip()
        # title likely appears as 'Title&nbsp;MGRM' -> keep the part after 'Title'
        title = None
        mt = re.search(r'(?i)Title\s*[:\s]*([^\s<]+)', t)
        if mt:
            title = mt.group(1).strip()

        # name: take first plaintext chunk before a <span> or <div>
        name = None
        # remove leading/trailing tags and look for text
        # split by '<span' or '<div'
        parts = re.split(r'(?i)<\s*(?:span|div)[^>]*>', after, maxsplit=1)
        if parts:
            # the text before the next span/div may contain the name
            cand = re.sub(r'<[^>]+>', '', parts[0]).strip()
            if cand:
                name = re.sub(r'\s{2,}', ' ', cand)

        # address: look for a <div> with address inside the matched chunk
        addr = None
        mdiv = re.search(r'(?i)<div[^>]*>(.*?)</div>', after, re.DOTALL)
        if mdiv:
            raw_addr = mdiv.group(1)
            addr = _strip_tags_and_unescape(raw_addr)

        out.append({'title': title, 'name': name, 'address': addr})
    return out


def parse_detail_page(text: str, filing_values: Dict[str, str] = None) -> Dict[str, Optional[str]]:
    # Normalize whitespace
    t = re.sub(r"\r", "", text)
    t = re.sub(r"\n[ \t]+", "\n", t)

    data = {}
    if filing_values:
        # Map known labels to our keys
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

    # Addresses / blocks
    data["principal_address"] = extract_block(t, "Principal Address")
    data["mailing_address"] = extract_block(t, "Mailing Address")
    data["registered_agent"] = extract_block(t, "Registered Agent Name & Address")
    data["authorized_persons"] = extract_block(t, "Authorized Person(s) Detail")

    return data


def scrape_document(doc_number: str, page) -> Dict:
    # Ir a la página de búsqueda por document number
    base = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByDocumentNumber"
    page.goto(base)

    # Rellenar y enviar
    page.fill('input[name="SearchTerm"]', doc_number)
    # Click en el botón submit
    page.click('input[type="submit"]')

    # esperar navegación / resultado
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        time.sleep(1)

    content = page.content()

    # Chequear si apareció mensaje "Record Not Found"
    if "Record Not Found" in content or "Record Not Found" in page.inner_text("body"):
        return {"document_number": doc_number, "found": False}

    # De otra forma, parsear
    text = page.inner_text("body")

    # Extraer el bloque de Filing Information usando un selector DOM más confiable
    filing_values = None
    try:
        filing_div = page.query_selector("div.detailSection.filingInformation div")
        if filing_div:
            filing_text = filing_div.inner_text()
            filing_values = parse_label_value_block(filing_text)
    except Exception:
        filing_values = None

    parsed = parse_detail_page(text, filing_values=filing_values)

    # Recopilar secciones DOM para extracción más precisa
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
                    # skip header row if it contains 'Report Year' or similar
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

    # Principal address + fecha de cambio
    pr = sections.get('Principal Address')
    if pr:
        parsed['principal_address'] = pr.get('div_text') or '\n'.join(pr.get('spans', []))
        # buscar 'Changed: '
        for s in pr.get('spans', []):
            if 'Changed' in s:
                m = re.search(r'Changed:\s*(\d{2}/\d{2}/\d{4})', s)
                if m:
                    parsed['principal_changed'] = m.group(1)
                    break

    # Mailing address
    maddr = sections.get('Mailing Address')
    if maddr:
        parsed['mailing_address'] = maddr.get('div_text') or '\n'.join(maddr.get('spans', []))

    # Registered Agent: separar nombre y dirección
    ragent = sections.get('Registered Agent Name & Address') or sections.get('Registered Agent Name &amp; Address')
    if ragent:
        name = None
        if ragent.get('spans'):
            # suele tener el nombre en la primera span
            name = ragent['spans'][0]
        addr = ragent.get('div_text')
        parsed['registered_agent_name'] = name
        parsed['registered_agent_address'] = addr
        # eliminar clave antigua si existe
        if 'registered_agent' in parsed:
            del parsed['registered_agent']

    # Authorized persons: extraer múltiples entradas correctamente desde el DOM
    authorized = []
    try:
        for sec in page.query_selector_all('div.detailSection'):
            try:
                spans = sec.query_selector_all('span')
                header = spans[0].inner_text().strip() if spans else ''
                if header == 'Authorized Person(s) Detail':
                    # Evalúa en el navegador una rutina más robusta para extraer múltiples entradas
                    authorized = sec.evaluate('''el => {
                        const out = [];
                        const spans = Array.from(el.querySelectorAll('span'));
                        for (let i = 0; i < spans.length; i++) {
                            const s = spans[i];
                            const txt = s.textContent.trim();
                            if (/^Title\b/i.test(txt)) {
                                const title = txt.replace(/^Title\b[:\s]*/i, '').trim() || null;
                                // buscar el nombre y la direccion tras este span
                                let name = null;
                                let address = null;
                                // look at next siblings of this span
                                let node = s.nextSibling;
                                while (node) {
                                    // text node -> possible name
                                    if (node.nodeType === Node.TEXT_NODE) {
                                        const t = node.textContent.trim();
                                        if (t) {
                                            // name usually uppercase and contains comma
                                            if (!name) name = t;
                                        }
                                    }
                                    // element node
                                    if (node.nodeType === Node.ELEMENT_NODE) {
                                        const ne = node;
                                        // if element is span containing a div, that's address
                                        if (ne.tagName === 'SPAN') {
                                            const d = ne.querySelector('div');
                                            if (d) {
                                                address = d.innerText.replace(/\r/g,'\n').trim();
                                                address = address.replace(/\n\s+/g,'\n');
                                                // done for this person
                                                break;
                                            }
                                            // otherwise maybe span contains the name
                                            const t2 = ne.textContent.trim();
                                            if (t2 && !/^Title\b/i.test(t2) && !/^Name & Address/i.test(t2)) {
                                                if (!name) name = t2;
                                            }
                                        }
                                        // if element is div directly, might be address
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

    # If browser-evaluation didn't yield results, try parsing the stored inner_html
    if not authorized:
        sec_auth = sections.get('Authorized Person(s) Detail')
        if sec_auth and sec_auth.get('inner_html'):
            try:
                authorized = parse_authorized_persons_from_html(sec_auth.get('inner_html'))
            except Exception:
                authorized = []

    parsed['authorized_persons'] = authorized

    # Annual Reports desde la tabla (si existe)
    asec = sections.get('Annual Reports')
    annual_reports_table = []
    if asec and asec.get('tables'):
        for rows in asec.get('tables'):
            for r in rows:
                annual_reports_table.append({'year': r.get('c1'), 'filed_date': r.get('c2')})
    # Si no hubo tabla, usamos la heurística anterior (document_images) — se llenará abajo
    parsed['annual_reports_table'] = annual_reports_table

    # Extraer anchors para Document Images / Annual Reports (mantener hrefs y pdfs)
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
        # Detectar enlaces que parezcan items de documento (fechas + label)
        if "-- ANNUAL REPORT" in up or "ANNUAL REPORT" in up or re.match(r"\d{2}/\d{2}/\d{4}", txt):
            # buscar el link PDF cercano (por lo general el siguiente anchor con texto 'View image in PDF format')
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

    # Normalizar enlaces relativos añadiendo prefijo
    base = 'https://search.sunbiz.org'
    for it in document_images:
        if it.get('link') and it['link'].startswith('/'):
            it['link'] = base + it['link']
        if it.get('pdf') and it['pdf'].startswith('/'):
            it['pdf'] = base + it['pdf']

    # También normalizar annual_reports entries extraídas vía heurística
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

    # Merge table-based annual reports if present (prefer table)
    if annual_reports_table:
        parsed['annual_reports'] = annual_reports_table
    else:
        parsed['annual_reports'] = annual_reports

    parsed["document_images"] = document_images
    parsed["document_number_searched"] = doc_number
    parsed["found"] = True
    return parsed


def main(headless: bool = True):
    out_path = "results.jsonl"
    # Open file and truncate existing contents, then write each result as we go
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        with open(out_path, "w", encoding="utf-8") as f:
            count = 0
            for doc in DOCUMENT_NUMBERS:
                print(f"Scraping {doc}...")
                try:
                    row = scrape_document(doc, page)
                except Exception as e:
                    row = {"document_number": doc, "error": str(e), "found": False}

                # write immediately
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                count += 1

                # short delay between requests
                time.sleep(1)

        browser.close()

    print(f"Saved {count} results to {out_path}")
    # Convert JSONL to CSV
    # csv_path = "results.csv"
    # write_csv_from_jsonl(out_path, csv_path)
    # print(f"Saved CSV to {csv_path}")


def write_csv_from_jsonl(jsonl_path: str, csv_path: str):
    rows = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            rows.append(obj)

    # Helper to make addresses single-line for CSV
    def _one_line(s: Optional[str]) -> Optional[str]:
        if not s:
            return None
        return " | ".join([ln.strip() for ln in s.splitlines() if ln.strip()])

    # Define CSV headers (more structured)
    headers = [
        "document_number",
        "document_number_searched",
        "fei_ein",
        "date_filed",
        "state",
        "status",
        "last_event",
        "event_date_filed",
        "principal_changed",
        "principal_address_singleline",
        "mailing_address_singleline",
        "registered_agent_name",
        "registered_agent_address_singleline",
        "authorized_count",
        "authorized_people",
        "authorized_persons_json",
        "annual_reports",
        "document_images_pdfs",
    ]

    with open(csv_path, "w", encoding="utf-8", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=headers)
        writer.writeheader()
        for obj in rows:
            # Normalize addresses
            p_addr = _one_line(obj.get("principal_address"))
            m_addr = _one_line(obj.get("mailing_address"))
            r_addr = _one_line(obj.get("registered_agent_address"))

            # Authorized persons
            auth_list = obj.get("authorized_persons") or []
            auth_people_parts = []
            for a in auth_list:
                name = (a.get("name") or "").strip()
                title = (a.get("title") or "").strip()
                addr = _one_line(a.get("address") or "") or ""
                part = name
                if title:
                    part += f" ({title})"
                if addr:
                    part += f" | {addr}"
                auth_people_parts.append(part)

            authorized_people = "; ".join([p for p in auth_people_parts if p])
            authorized_count = len(auth_list)
            authorized_json = json.dumps(auth_list, ensure_ascii=False)

            # Annual reports (flattened)
            ars = obj.get("annual_reports") or []
            ar_strs = []
            for ar in ars:
                if isinstance(ar, dict):
                    y = ar.get("year") or ""
                    d = ar.get("filed_date") or ""
                    ar_strs.append(f"{y}:{d}" if y or d else "")
                else:
                    ar_strs.append(str(ar))

            # Document images PDFs
            imgs = obj.get("document_images") or []
            img_pdfs = []
            for im in imgs:
                pdf = im.get("pdf") or im.get("link") or ""
                img_pdfs.append(pdf)

            row = {
                "document_number": obj.get("document_number"),
                "document_number_searched": obj.get("document_number_searched"),
                "fei_ein": obj.get("fei_ein"),
                "date_filed": obj.get("date_filed"),
                "state": obj.get("state"),
                "status": obj.get("status"),
                "last_event": obj.get("last_event"),
                "event_date_filed": obj.get("event_date_filed"),
                "principal_changed": obj.get("principal_changed"),
                "principal_address_singleline": p_addr,
                "mailing_address_singleline": m_addr,
                "registered_agent_name": obj.get("registered_agent_name"),
                "registered_agent_address_singleline": r_addr,
                "authorized_count": authorized_count,
                "authorized_people": authorized_people,
                "authorized_persons_json": authorized_json,
                "annual_reports": "; ".join([s for s in ar_strs if s]),
                "document_images_pdfs": "; ".join([s for s in img_pdfs if s]),
            }

            writer.writerow(row)


if __name__ == "__main__":
    # Cambia headless=False para ver el navegador
    main(headless=True)
