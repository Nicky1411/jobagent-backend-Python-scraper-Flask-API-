"""
JobAgent EU - Backend
Sources: Arbeitnow, Remotive, Adzuna, WeWorkRemotely, The Muse, Stepstone, Bundesagentur
"""
import time, re, logging, os, json
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
app = Flask(__name__)
CORS(app, origins=["*"])

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

# ── Claude API ────────────────────────────────────────
def call_claude(prompt, system="", max_tokens=1000):
    if not ANTHROPIC_KEY:
        raise Exception("ANTHROPIC_KEY not set")
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-5", "max_tokens": max_tokens,
              "system": system or "You are an expert career advisor.",
              "messages": [{"role": "user", "content": prompt}]},
        timeout=60)
    if r.status_code != 200:
        raise Exception(f"Claude API error {r.status_code}: {r.text[:200]}")
    return r.json()["content"][0]["text"]

# ── File extraction ───────────────────────────────────
def extract_text_from_file(file_bytes, filename):
    ext = filename.lower().split(".")[-1]
    if ext == "pdf":
        import fitz
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            return "\n".join(page.get_text() for page in doc).strip()
    elif ext in ["docx", "doc"]:
        from docx import Document
        from io import BytesIO
        doc = Document(BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    elif ext == "txt":
        return file_bytes.decode("utf-8", errors="ignore").strip()
    else:
        raise Exception(f"Unsupported file type: {ext}. Use PDF, DOCX, or TXT.")

# ── SOURCE 1: Arbeitnow (free API) ───────────────────
def fetch_arbeitnow(keywords):
    results = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        kw = keywords.lower().split()
        for job in r.json().get("data", []):
            if any(k in f"{job.get('title','')} {job.get('description','')}".lower() for k in kw):
                results.append({
                    "id": f"arb_{job.get('slug', len(results))}",
                    "title": job.get("title", ""), "company": job.get("company_name", ""),
                    "location": job.get("location", "Europe"), "salary": "Competitive",
                    "url": job.get("url", ""), "source": "Arbeitnow",
                    "tags": job.get("tags", [])[:4] + (["Visa Sponsor"] if job.get("visa_sponsored") else []),
                    "posted": "Recently", "description": job.get("description", "")[:400], "match": 0,
                })
        log.info(f"Arbeitnow: {len(results)} jobs")
    except Exception as e: log.error(f"Arbeitnow: {e}")
    return results

# ── SOURCE 2: Remotive (free API) ────────────────────
def fetch_remotive(keywords):
    results = []
    try:
        r = requests.get(f"https://remotive.com/api/remote-jobs?search={quote_plus(keywords)}&limit=20", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        for job in r.json().get("jobs", []):
            results.append({
                "id": f"rem_{job.get('id', len(results))}",
                "title": job.get("title", ""), "company": job.get("company_name", ""),
                "location": job.get("candidate_required_location", "Remote/Europe"),
                "salary": job.get("salary", "Competitive"), "url": job.get("url", ""),
                "source": "Remotive",
                "tags": job.get("tags", [])[:4] + ["Remote", "English Only"],
                "posted": job.get("publication_date", "")[:10],
                "description": BeautifulSoup(job.get("description", ""), "lxml").get_text()[:400],
                "match": 0,
            })
        log.info(f"Remotive: {len(results)} jobs")
    except Exception as e: log.error(f"Remotive: {e}")
    return results

# ── SOURCE 3: Adzuna (free API with key) ─────────────
def fetch_adzuna(keywords, app_id="", app_key=""):
    if not app_id or not app_key: return []
    results = []
    try:
        for country in ["nl", "de", "gb", "at", "ch"]:
            r = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
                f"?app_id={app_id}&app_key={app_key}&results_per_page=8"
                f"&what={quote_plus(keywords)}&content-type=application/json",
                timeout=15)
            if r.status_code != 200: continue
            for job in r.json().get("results", []):
                results.append({
                    "id": f"adz_{country}_{job.get('id', len(results))}",
                    "title": job.get("title", ""), "company": job.get("company", {}).get("display_name", ""),
                    "location": job.get("location", {}).get("display_name", ""),
                    "salary": f"€{int(job.get('salary_min',0)):,}–€{int(job.get('salary_max',0)):,}" if job.get("salary_min") else "Competitive",
                    "url": job.get("redirect_url", ""), "source": f"Adzuna ({country.upper()})",
                    "tags": ["English Friendly"],
                    "posted": job.get("created", "")[:10],
                    "description": job.get("description", "")[:400], "match": 0,
                })
        log.info(f"Adzuna: {len(results)} jobs")
    except Exception as e: log.error(f"Adzuna: {e}")
    return results

# ── SOURCE 4: WeWorkRemotely (RSS) ────────────────────
def fetch_weworkremotely(keywords):
    results = []
    try:
        # WWR has category-specific RSS feeds
        feeds = [
            "https://weworkremotely.com/remote-jobs.rss",
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-management-finance-jobs.rss",
            "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
            "https://weworkremotely.com/categories/remote-data-science-ai-jobs.rss",
        ]
        kw = keywords.lower().split()
        seen = set()
        for feed_url in feeds:
            r = requests.get(feed_url, headers=HEADERS, timeout=15)
            if r.status_code != 200: continue
            try:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    title = item.findtext("title", "").strip()
                    link = item.findtext("link", "").strip()
                    desc = BeautifulSoup(item.findtext("description", ""), "lxml").get_text()[:400]
                    region = item.findtext("{https://weworkremotely.com}region", "Worldwide")
                    company = ""
                    # Extract company from title (format: "Role at Company")
                    if " at " in title:
                        parts = title.split(" at ", 1)
                        title = parts[0].strip()
                        company = parts[1].strip()
                    text = f"{title} {desc}".lower()
                    if any(k in text for k in kw) and link not in seen:
                        seen.add(link)
                        results.append({
                            "id": f"wwr_{len(results)}",
                            "title": title, "company": company,
                            "location": region or "Remote / Worldwide",
                            "salary": "Competitive", "url": link,
                            "source": "WeWorkRemotely",
                            "tags": ["Remote", "English Only", "Worldwide"],
                            "posted": item.findtext("pubDate", "")[:16],
                            "description": desc, "match": 0,
                        })
            except ET.ParseError as pe:
                log.warning(f"WWR RSS parse error: {pe}")
        log.info(f"WeWorkRemotely: {len(results)} jobs")
    except Exception as e: log.error(f"WeWorkRemotely: {e}")
    return results

# ── SOURCE 5: The Muse (free API) ────────────────────
def fetch_themuse(keywords, page=0):
    results = []
    try:
        # The Muse categories
        categories = ["Data Science", "Software Engineer", "Product", "Operations", "Finance",
                      "Strategy", "Business Development", "Engineering", "Consulting", "Management"]
        kw_lower = keywords.lower()
        # Pick relevant category based on keywords
        selected_cats = [c for c in categories if any(w in kw_lower for w in c.lower().split())]
        if not selected_cats:
            selected_cats = ["Software Engineer", "Operations"]  # fallback

        seen = set()
        for cat in selected_cats[:2]:  # max 2 categories
            url = f"https://www.themuse.com/api/public/jobs?category={quote_plus(cat)}&level=Senior+Level&level=Mid+Level&page={page}&descending=true"
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200: continue
            data = r.json()
            for job in data.get("results", []):
                title = job.get("name", "")
                company = job.get("company", {}).get("name", "")
                locations = job.get("locations", [{}])
                location = locations[0].get("name", "Remote") if locations else "Remote"
                job_url = job.get("refs", {}).get("landing_page", "")
                desc = BeautifulSoup(job.get("contents", ""), "lxml").get_text()[:400]
                text = f"{title} {desc}".lower()
                kw_list = keywords.lower().split()
                if any(k in text for k in kw_list) and job_url not in seen:
                    seen.add(job_url)
                    results.append({
                        "id": f"muse_{job.get('id', len(results))}",
                        "title": title, "company": company,
                        "location": location, "salary": "Competitive",
                        "url": job_url, "source": "The Muse",
                        "tags": [cat, "English Only"],
                        "posted": job.get("publication_date", "")[:10],
                        "description": desc, "match": 0,
                    })
        log.info(f"The Muse: {len(results)} jobs")
    except Exception as e: log.error(f"The Muse: {e}")
    return results

# ── SOURCE 6: Stepstone Germany (scraping) ───────────
def fetch_stepstone(keywords, location="Deutschland"):
    results = []
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        url = f"https://www.stepstone.de/jobsuche/?q={quote_plus(keywords)}&where={quote_plus(location)}&radius=30&lang=en_GB"
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            log.warning(f"Stepstone returned {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "lxml")
        # Stepstone job card selectors (they update these periodically)
        cards = (soup.select("article[data-at='job-item']") or
                 soup.select("[data-genesis-element='BASE']") or
                 soup.select("article.res-1v9xmhz") or
                 soup.select("[class*='ResultItem']") or
                 soup.select("article"))
        log.info(f"Stepstone: found {len(cards)} raw cards")
        for card in cards[:15]:
            try:
                title_el = (card.select_one("h2 a") or card.select_one("h3 a") or
                            card.select_one("[data-at='job-item-title']") or card.select_one("a[href*='stellenangebote']"))
                company_el = (card.select_one("[data-at='job-item-company-name']") or
                              card.select_one("[class*='company']") or card.select_one("span[class*='Company']"))
                location_el = (card.select_one("[data-at='job-item-location']") or
                               card.select_one("[class*='location']"))
                if not title_el: continue
                href = title_el.get("href", "")
                full_url = f"https://www.stepstone.de{href}" if href.startswith("/") else href
                results.append({
                    "id": f"ss_{len(results)}",
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else location,
                    "salary": "Competitive", "url": full_url,
                    "source": "Stepstone 🇩🇪",
                    "tags": ["Germany", "English Friendly"],
                    "posted": "Recently",
                    "description": card.get_text(strip=True)[:400], "match": 0,
                })
            except Exception as e:
                log.debug(f"Stepstone card error: {e}")
        log.info(f"Stepstone: {len(results)} jobs parsed")
    except Exception as e: log.error(f"Stepstone: {e}")
    return results

# ── SOURCE 7: Bundesagentur (official German govt API) ─
def fetch_bundesagentur(keywords, location=""):
    results = []
    try:
        params = {
            "was": keywords,
            "wo": location or "Deutschland",
            "page": 0,
            "size": 10,
            "sprache": "englisch",  # English language filter
        }
        r = requests.get(
            "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs",
            params=params,
            headers={**HEADERS, "X-API-Key": "jobboerse-jobsuche"},
            timeout=15)
        if r.status_code != 200:
            log.warning(f"Bundesagentur returned {r.status_code}")
            return []
        data = r.json()
        for job in data.get("stellenangebote", []):
            results.append({
                "id": f"ba_{job.get('refnr', len(results))}",
                "title": job.get("titel", ""),
                "company": job.get("arbeitgeber", ""),
                "location": job.get("arbeitsort", {}).get("ort", "Germany"),
                "salary": "Competitive",
                "url": f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{job.get('refnr','')}",
                "source": "Bundesagentur 🇩🇪",
                "tags": ["Germany", "Official"],
                "posted": job.get("eintrittsdatum", "Recently"),
                "description": job.get("stellenbeschreibung", "")[:400],
                "match": 0,
            })
        log.info(f"Bundesagentur: {len(results)} jobs")
    except Exception as e: log.error(f"Bundesagentur: {e}")
    return results

# ── Scoring (profession-agnostic) ────────────────────
def score_job(job, profile):
    score = 30
    skills = [s.lower() for s in profile.get("skills", [])]
    profile_title = (profile.get("title") or "").lower()
    title = job["title"].lower()
    combined = title + " " + (job.get("description","") + " ".join(job.get("tags",[]))).lower()

    # Skill overlap — core signal, works for any profession
    matched = sum(1 for s in skills if len(s) > 3 and s.lower() in combined)
    score += min(matched * 6, 40)

    # Title word overlap
    title_words = set(title.split())
    profile_words = set(profile_title.split())
    score += len(title_words & profile_words) * 5

    # Seniority match
    for level in ["senior","lead","principal","staff","head","director","manager"]:
        if level in profile_title and level in title: score += 8; break
        elif level in profile_title and level not in title: score -= 3

    # Experience
    exp = profile.get("experience_years", 0)
    if exp >= 5 and any(w in title for w in ["junior","intern","graduate","entry"]): score -= 20
    elif exp >= 4: score += 4

    # Location bonus
    location = job.get("location","").lower()
    if any(loc in location for loc in ["netherlands","germany","amsterdam","berlin","munich","hamburg","stockholm","vienna","zurich","europe","remote","worldwide"]): score += 8

    # Visa/relocation — critical for expats
    if any(t in ["Visa Sponsor","Relocation Package","Visa Sponsorship"] for t in job.get("tags",[])): score += 12
    if any(t in ["English Only","English Friendly","English OK"] for t in job.get("tags",[])): score += 10

    return max(0, min(score, 99))

def dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        k = f"{j['title'].lower().strip()}|{j['company'].lower().strip()}"
        if k not in seen: seen.add(k); out.append(j)
    return out

# ── Routes ────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status": "ok", "claude": bool(ANTHROPIC_KEY),
        "sources": ["arbeitnow","remotive","adzuna","weworkremotely","themuse","stepstone","bundesagentur"],
        "ts": datetime.now().isoformat()
    })

@app.route("/parse", methods=["POST"])
def parse_resume():
    resume_text = ""
    if "file" in request.files:
        f = request.files["file"]
        try:
            resume_text = extract_text_from_file(f.read(), f.filename or "resume")
            log.info(f"Extracted {len(resume_text)} chars from {f.filename}")
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    elif request.is_json:
        resume_text = (request.get_json() or {}).get("text","").strip()
    if not resume_text or len(resume_text) < 20:
        return jsonify({"error": "Could not extract text. Try a different format or paste text manually."}), 400
    try:
        result = call_claude(
            f'Extract info from this resume. Return ONLY a JSON object. Keep values concise.\n\n{{"name":"","email":"","phone":"","title":"","summary":"one sentence","experience_years":0,"skills":[],"experience":[{{"company":"","role":"","duration":"","bullets":[]}}],"education":"","certifications":[],"languages":[]}}\n\nRESUME:\n{resume_text[:5000]}',
            "Return ONLY the filled JSON object. No markdown. No explanation. Keep all string values concise.", 1500)
        cleaned = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(cleaned)
        return jsonify({"success": True, "profile": parsed, "extracted_chars": len(resume_text)})
    except Exception as e:
        log.error(f"Parse error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate_content():
    body = request.get_json() or {}
    content_type = body.get("type","cover")
    job = body.get("job",{}); profile = body.get("profile",{})
    try:
        if content_type == "cover":
            result = call_claude(
                f"Write a professional 3-paragraph cover letter for:\nJob: {job.get('title')} at {job.get('company')}, {job.get('location')}\nDescription: {job.get('description','')[:300]}\nCandidate: {profile.get('title')}, {profile.get('experience_years')} years, Skills: {', '.join(profile.get('skills',[])[:8])}\nTone: professional, warm. English only. End with availability to interview.",
                "You are an expert cover letter writer for international job applications.", 800)
        else:
            result = call_claude(
                f"Tailor this resume for the job below.\nJob: {job.get('title')} at {job.get('company')}\nDescription: {job.get('description','')[:300]}\nCandidate: {profile.get('title')}, {profile.get('experience_years')} yrs, Skills: {', '.join(profile.get('skills',[])[:10])}\nOutput: 1) Professional Summary (3 sentences) 2) Top 8 tailored skills 3) Rewritten experience bullets",
                "You are an expert resume writer for international job applications.", 1200)
        return jsonify({"success": True, "content": result})
    except Exception as e:
        log.error(f"Generate error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/search", methods=["POST"])
def search_jobs():
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

    b = request.get_json() or {}
    kw = b.get("keywords","senior engineer europe")
    prof = b.get("profile",{})
    sources = b.get("sources",["arbeitnow","remotive","adzuna","weworkremotely","themuse","stepstone"])
    aid = b.get("adzuna_app_id","") or os.environ.get("ADZUNA_APP_ID","")
    akey = b.get("adzuna_app_key","") or os.environ.get("ADZUNA_APP_KEY","")

    # Build smart fallback keywords from profile
    profile_title = (prof.get("title") or "").strip()
    top_skills = " ".join(prof.get("skills",[])[:2])
    fallback_kw = f"{profile_title} {top_skills}".strip() or kw

    # Define all fetch tasks
    tasks = []
    if "arbeitnow" in sources:
        tasks.append(("arbeitnow_main", lambda: fetch_arbeitnow(kw)))
        tasks.append(("arbeitnow_fallback", lambda: fetch_arbeitnow(fallback_kw)))
    if "remotive" in sources:
        tasks.append(("remotive_main", lambda: fetch_remotive(kw)))
        tasks.append(("remotive_fallback", lambda: fetch_remotive(profile_title or "senior engineer")))
    if "adzuna" in sources and aid and akey:
        tasks.append(("adzuna_main", lambda: fetch_adzuna(kw, aid, akey)))
        if profile_title:
            tasks.append(("adzuna_title", lambda: fetch_adzuna(profile_title, aid, akey)))
    if "weworkremotely" in sources:
        tasks.append(("wwr", lambda: fetch_weworkremotely(kw)))
    if "themuse" in sources:
        tasks.append(("themuse", lambda: fetch_themuse(kw)))
    if "stepstone" in sources:
        tasks.append(("stepstone", lambda: fetch_stepstone(kw)))
    if "bundesagentur" in sources:
        tasks.append(("bundesagentur", lambda: fetch_bundesagentur(kw)))

    # Run all tasks in parallel with 20s timeout each
    all_jobs = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(fn): name for name, fn in tasks}
        for future in as_completed(future_map, timeout=25):
            name = future_map[future]
            try:
                result = future.result(timeout=5)
                all_jobs += result
                log.info(f"{name}: {len(result)} jobs")
            except Exception as e:
                log.warning(f"{name} failed: {e}")

    all_jobs = dedup(all_jobs)
    for j in all_jobs: j["match"] = score_job(j, prof)
    all_jobs.sort(key=lambda j: j["match"], reverse=True)
    log.info(f"Total: {len(all_jobs)} jobs from {len(sources)} sources")
    return jsonify({"jobs": all_jobs, "total": len(all_jobs), "sources_used": sources})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*50}\n  JobAgent EU Backend — port {port}\n  Claude: {'OK' if ANTHROPIC_KEY else 'MISSING'}\n  Sources: 7 job boards\n{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
