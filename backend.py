"""
JobAgent EU - Backend (stable)
"""
import time, re, logging, os, json
import xml.etree.ElementTree as ET
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9"
}
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

# ── Claude ────────────────────────────────────────────
def call_claude(prompt, system="", max_tokens=1000):
    if not ANTHROPIC_KEY:
        raise Exception("ANTHROPIC_KEY not set")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": max_tokens,
            "system": system or "You are an expert career advisor.",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise Exception("Claude API error {}: {}".format(r.status_code, r.text[:200]))
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
        raise Exception("Unsupported file type: {}. Use PDF, DOCX, or TXT.".format(ext))

# ── Job Sources ───────────────────────────────────────
def fetch_arbeitnow(keywords):
    results = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        kw = keywords.lower().split()
        broad = kw + ["engineer", "manager", "consultant", "analyst", "developer", "data", "cloud"]
        for job in r.json().get("data", []):
            text = "{} {}".format(job.get("title",""), job.get("description","")).lower()
            if any(k in text for k in broad):
                results.append({
                    "id": "arb_{}".format(job.get("slug", len(results))),
                    "title": job.get("title", ""),
                    "company": job.get("company_name", ""),
                    "location": job.get("location", "Europe"),
                    "salary": "Competitive",
                    "url": job.get("url", ""),
                    "source": "Arbeitnow",
                    "tags": job.get("tags", [])[:4] + (["Visa Sponsor"] if job.get("visa_sponsored") else []),
                    "posted": "Recently",
                    "description": job.get("description", "")[:400],
                    "match": 0,
                })
    except Exception as e:
        log.error("Arbeitnow: {}".format(e))
    return results

def fetch_remotive(keywords):
    results = []
    try:
        r = requests.get(
            "https://remotive.com/api/remote-jobs?search={}&limit=20".format(quote_plus(keywords)),
            headers=HEADERS, timeout=15
        )
        if r.status_code != 200:
            return []
        for job in r.json().get("jobs", []):
            results.append({
                "id": "rem_{}".format(job.get("id", len(results))),
                "title": job.get("title", ""),
                "company": job.get("company_name", ""),
                "location": job.get("candidate_required_location", "Remote/Europe"),
                "salary": job.get("salary", "Competitive"),
                "url": job.get("url", ""),
                "source": "Remotive",
                "tags": job.get("tags", [])[:4] + ["Remote", "English Only"],
                "posted": job.get("publication_date", "")[:10],
                "description": BeautifulSoup(job.get("description", ""), "lxml").get_text()[:400],
                "match": 0,
            })
    except Exception as e:
        log.error("Remotive: {}".format(e))
    return results

def fetch_weworkremotely(keywords):
    results = []
    try:
        feeds = [
            "https://weworkremotely.com/remote-jobs.rss",
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "https://weworkremotely.com/categories/remote-management-finance-jobs.rss",
            "https://weworkremotely.com/categories/remote-data-science-ai-jobs.rss",
        ]
        kw = keywords.lower().split()
        seen = set()
        for feed_url in feeds:
            r = requests.get(feed_url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            try:
                root = ET.fromstring(r.content)
                for item in root.findall(".//item"):
                    title = item.findtext("title", "").strip()
                    link = item.findtext("link", "").strip()
                    desc = BeautifulSoup(item.findtext("description", ""), "lxml").get_text()[:400]
                    company = ""
                    if " at " in title:
                        parts = title.split(" at ", 1)
                        title = parts[0].strip()
                        company = parts[1].strip()
                    text = "{} {}".format(title, desc).lower()
                    if any(k in text for k in kw) and link not in seen:
                        seen.add(link)
                        results.append({
                            "id": "wwr_{}".format(len(results)),
                            "title": title,
                            "company": company,
                            "location": "Remote / Worldwide",
                            "salary": "Competitive",
                            "url": link,
                            "source": "WeWorkRemotely",
                            "tags": ["Remote", "English Only", "Worldwide"],
                            "posted": item.findtext("pubDate", "")[:16],
                            "description": desc,
                            "match": 0,
                        })
            except ET.ParseError:
                continue
    except Exception as e:
        log.error("WeWorkRemotely: {}".format(e))
    return results

def fetch_themuse(keywords):
    results = []
    try:
        kw_lower = keywords.lower()
        categories = [
            "Data Science", "Software Engineer", "Product", "Operations",
            "Finance", "Strategy", "Business Development", "Engineering",
            "Consulting", "Management", "Project Management",
        ]
        selected = [c for c in categories if any(w in kw_lower for w in c.lower().split())]
        if not selected:
            selected = ["Operations", "Software Engineer"]
        seen = set()
        for cat in selected[:3]:
            url = "https://www.themuse.com/api/public/jobs?category={}&level=Senior+Level&level=Mid+Level&page=0&descending=true".format(
                quote_plus(cat)
            )
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            for job in r.json().get("results", []):
                title = job.get("name", "")
                company = job.get("company", {}).get("name", "")
                locs = job.get("locations", [{}])
                location = locs[0].get("name", "Remote") if locs else "Remote"
                job_url = job.get("refs", {}).get("landing_page", "")
                desc = BeautifulSoup(job.get("contents", ""), "lxml").get_text()[:400]
                text = "{} {}".format(title, desc).lower()
                kw_list = keywords.lower().split()
                if any(k in text for k in kw_list) and job_url not in seen:
                    seen.add(job_url)
                    results.append({
                        "id": "muse_{}".format(job.get("id", len(results))),
                        "title": title,
                        "company": company,
                        "location": location,
                        "salary": "Competitive",
                        "url": job_url,
                        "source": "The Muse",
                        "tags": [cat, "English Only"],
                        "posted": job.get("publication_date", "")[:10],
                        "description": desc,
                        "match": 0,
                    })
    except Exception as e:
        log.error("The Muse: {}".format(e))
    return results

def fetch_adzuna(keywords, app_id="", app_key=""):
    if not app_id or not app_key:
        return []
    results = []
    try:
        for country in ["nl", "de", "gb", "at", "ch", "in", "sg"]:
            r = requests.get(
                "https://api.adzuna.com/v1/api/jobs/{}/search/1?app_id={}&app_key={}&results_per_page=8&what={}&content-type=application/json".format(
                    country, app_id, app_key, quote_plus(keywords)
                ),
                timeout=15,
            )
            if r.status_code != 200:
                continue
            for job in r.json().get("results", []):
                results.append({
                    "id": "adz_{}_{}".format(country, job.get("id", len(results))),
                    "title": job.get("title", ""),
                    "company": job.get("company", {}).get("display_name", ""),
                    "location": job.get("location", {}).get("display_name", ""),
                    "salary": "Competitive",
                    "url": job.get("redirect_url", ""),
                    "source": "Adzuna ({})".format(country.upper()),
                    "tags": ["English Friendly"],
                    "posted": job.get("created", "")[:10],
                    "description": job.get("description", "")[:400],
                    "match": 0,
                })
    except Exception as e:
        log.error("Adzuna: {}".format(e))
    return results

def fetch_stepstone(keywords):
    results = []
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        url = "https://www.stepstone.de/jobsuche/?q={}&where=Deutschland&radius=30&lang=en_GB".format(
            quote_plus(keywords)
        )
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = (
            soup.select("article[data-at='job-item']") or
            soup.select("[data-genesis-element='BASE']") or
            soup.select("article")
        )
        for card in cards[:15]:
            try:
                title_el = card.select_one("h2 a, h3 a, [data-at='job-item-title']")
                company_el = card.select_one("[data-at='job-item-company-name'], [class*='company']")
                location_el = card.select_one("[data-at='job-item-location'], [class*='location']")
                if not title_el:
                    continue
                href = title_el.get("href", "")
                results.append({
                    "id": "ss_{}".format(len(results)),
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "Germany",
                    "salary": "Competitive",
                    "url": "https://www.stepstone.de{}".format(href) if href.startswith("/") else href,
                    "source": "Stepstone DE",
                    "tags": ["Germany", "English Friendly"],
                    "posted": "Recently",
                    "description": card.get_text(strip=True)[:400],
                    "match": 0,
                })
            except Exception:
                continue
    except Exception as e:
        log.error("Stepstone: {}".format(e))
    return results

# ── Scoring ───────────────────────────────────────────

def fetch_naukri(keywords):
    """Scrape Naukri.com — India's #1 job board."""
    results = []
    try:
        from playwright.sync_api import sync_playwright
        query = quote_plus(keywords)
        url = "https://www.naukri.com/{}-jobs?k={}&nignbevent_src=jobsearchDesk".format(
            keywords.lower().replace(" ", "-"), query
        )
        log.info("Scraping Naukri: {}".format(url))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("article.jobTuple, .jobTupleHeader, [class*='job-tuple'], .cust-job-tuple")
        log.info("Naukri: {} raw cards".format(len(cards)))
        for card in cards[:20]:
            try:
                title_el = card.select_one("a.title, .jobTitle, [class*='jobTitle'], h2 a")
                company_el = card.select_one(".companyInfo a, .comp-name, [class*='comp-name']")
                location_el = card.select_one(".location, .locWdth, [class*='location']")
                salary_el = card.select_one(".salary, [class*='salary']")
                link_el = card.select_one("a[href*='naukri.com']") or title_el
                if not title_el: continue
                href = link_el.get("href", "") if link_el else ""
                results.append({
                    "id": "naukri_{}".format(len(results)),
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "India",
                    "salary": salary_el.get_text(strip=True) if salary_el else "Competitive",
                    "url": href if href.startswith("http") else "https://www.naukri.com" + href,
                    "source": "Naukri 🇮🇳",
                    "tags": ["India", "English"],
                    "posted": "Recently",
                    "description": card.get_text(strip=True)[:400],
                    "match": 0,
                })
            except Exception:
                continue
        log.info("Naukri: {} jobs".format(len(results)))
    except Exception as e:
        log.error("Naukri: {}".format(e))
    return results


def fetch_iimjobs(keywords):
    """Scrape IIMJobs — premium Indian jobs for MBAs, strategy, consulting."""
    results = []
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        url = "https://www.iimjobs.com/search/?search={}".format(quote_plus(keywords))
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select(".job-listings .job, article.job, .jobCard, [class*='job-item']")
        log.info("IIMJobs: {} raw cards".format(len(cards)))
        for card in cards[:20]:
            try:
                title_el = card.select_one("h2 a, h3 a, .job-title a, a.title")
                company_el = card.select_one(".company, .comp-name, [class*='company']")
                location_el = card.select_one(".location, [class*='location']")
                link_el = card.select_one("a[href]")
                if not title_el: continue
                href = link_el.get("href", "") if link_el else ""
                results.append({
                    "id": "iimj_{}".format(len(results)),
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "India",
                    "salary": "Competitive",
                    "url": href if href.startswith("http") else "https://www.iimjobs.com" + href,
                    "source": "IIMJobs 🇮🇳",
                    "tags": ["India", "Senior", "MBA", "English"],
                    "posted": "Recently",
                    "description": card.get_text(strip=True)[:400],
                    "match": 0,
                })
            except Exception:
                continue
        log.info("IIMJobs: {} jobs".format(len(results)))
    except Exception as e:
        log.error("IIMJobs: {}".format(e))
    return results


def fetch_instahyre(keywords):
    """Fetch from Instahyre — curated Indian startup/tech jobs."""
    results = []
    try:
        url = "https://www.instahyre.com/search-jobs/?q={}".format(quote_plus(keywords))
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.select(".opportunity-card, [class*='job-card'], .job-listing")
        log.info("Instahyre: {} raw cards".format(len(cards)))
        for card in cards[:20]:
            try:
                title_el = card.select_one("h2, h3, .role-title, [class*='title']")
                company_el = card.select_one(".company, [class*='company']")
                location_el = card.select_one(".location, [class*='location']")
                link_el = card.select_one("a[href]")
                if not title_el: continue
                href = link_el.get("href", "") if link_el else ""
                results.append({
                    "id": "ih_{}".format(len(results)),
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "India",
                    "salary": "Competitive",
                    "url": href if href.startswith("http") else "https://www.instahyre.com" + href,
                    "source": "Instahyre 🇮🇳",
                    "tags": ["India", "Startup", "Tech"],
                    "posted": "Recently",
                    "description": card.get_text(strip=True)[:400],
                    "match": 0,
                })
            except Exception:
                continue
        log.info("Instahyre: {} jobs".format(len(results)))
    except Exception as e:
        log.error("Instahyre: {}".format(e))
    return results

def score_job(job, profile):
    score = 30
    skills = [s.lower() for s in profile.get("skills", [])]
    profile_title = (profile.get("title") or "").lower()
    title = job["title"].lower()
    combined = title + " " + (job.get("description", "") + " ".join(job.get("tags", []))).lower()
    matched = sum(1 for s in skills if len(s) > 3 and s.lower() in combined)
    score += min(matched * 6, 40)
    title_words = set(title.split())
    profile_words = set(profile_title.split())
    score += len(title_words & profile_words) * 5
    for level in ["senior", "lead", "principal", "staff", "head", "director", "manager"]:
        if level in profile_title and level in title:
            score += 8
            break
        elif level in profile_title and level not in title:
            score -= 3
    exp = profile.get("experience_years", 0)
    if exp >= 5 and any(w in title for w in ["junior", "intern", "graduate", "entry"]):
        score -= 20
    elif exp >= 4:
        score += 4
    location = job.get("location", "").lower()
    if any(loc in location for loc in ["netherlands", "germany", "amsterdam", "berlin",
                                        "munich", "hamburg", "stockholm", "vienna",
                                        "zurich", "europe", "remote", "worldwide",
                                        "india", "bangalore", "mumbai", "delhi",
                                        "hyderabad", "pune", "chennai", "bengaluru"]):
        score += 8
    if any(t in ["Visa Sponsor", "Relocation Package"] for t in job.get("tags", [])):
        score += 12
    if any(t in ["English Only", "English Friendly"] for t in job.get("tags", [])):
        score += 10
    return max(0, min(score, 99))

def claude_score_jobs(jobs, profile):
    if not jobs or not profile.get("name"):
        return jobs
    to_score = jobs[:15]
    rest = jobs[15:]
    profile_summary = "Title: {}\nExperience: {} years\nSkills: {}\nSummary: {}".format(
        profile.get("title", ""),
        profile.get("experience_years", 0),
        ", ".join(profile.get("skills", [])[:12]),
        profile.get("summary", ""),
    )
    jobs_text = ""
    for i, job in enumerate(to_score):
        jobs_text += "JOB {}: {} at {}, {}. {}---\n".format(
            i + 1, job["title"], job["company"], job["location"],
            job.get("description", "")[:150]
        )
    try:
        result = call_claude(
            "Score how well this candidate matches each job. Return ONLY a JSON array of {} objects.\n\nCANDIDATE:\n{}\n\nJOBS:\n{}\n\nReturn exactly:\n[{{\"score\": 85, \"reason\": \"Strong skills match\", \"highlight\": \"3 exact skill matches\"}}, ...]\n\nRules:\n- score 0-99 (85+=excellent, 70-84=good, 50-69=partial, <50=poor)\n- Be strict - 85+ only for genuinely strong matches\n- Consider skills, seniority, industry, location".format(
                len(to_score), profile_summary, jobs_text
            ),
            "Expert recruiter. Score job-candidate fit. Return ONLY valid JSON array. No markdown.",
            600,
        )
        cleaned = result.replace("```json", "").replace("```", "").strip()
        scores = json.loads(cleaned)
        for i, job in enumerate(to_score):
            if i < len(scores):
                s = scores[i]
                job["match"] = max(0, min(int(s.get("score", job["match"])), 99))
                job["match_reason"] = s.get("reason", "")
                job["match_highlight"] = s.get("highlight", "")
        log.info("Claude scored {} jobs".format(len(to_score)))
    except Exception as e:
        log.error("Claude scoring error: {}".format(e))
    return to_score + rest

def dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        k = "{}|{}".format(j["title"].lower().strip(), j["company"].lower().strip())
        if k not in seen:
            seen.add(k)
            out.append(j)
    return out

# ── Routes ────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "claude": bool(ANTHROPIC_KEY),
        "sources": ["arbeitnow", "remotive", "weworkremotely", "themuse", "adzuna", "stepstone"],
        "ts": datetime.now().isoformat(),
    })

@app.route("/parse", methods=["POST"])
def parse_resume():
    resume_text = ""
    if "file" in request.files:
        f = request.files["file"]
        try:
            resume_text = extract_text_from_file(f.read(), f.filename or "resume")
            log.info("Extracted {} chars from {}".format(len(resume_text), f.filename))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    elif request.is_json:
        resume_text = (request.get_json() or {}).get("text", "").strip()
    if not resume_text or len(resume_text) < 20:
        return jsonify({"error": "Could not extract text. Try a different format or paste text manually."}), 400
    try:
        result = call_claude(
            'Extract info from this resume. Return ONLY a JSON object. Keep values concise.\n\n{"name":"","email":"","phone":"","title":"","summary":"one sentence","experience_years":0,"skills":[],"experience":[{"company":"","role":"","duration":"","bullets":[]}],"education":"","certifications":[],"languages":[]}\n\nRESUME:\n' + resume_text[:5000],
            "Return ONLY the filled JSON object. No markdown. No explanation. Keep all string values concise.",
            1500,
        )
        cleaned = result.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        return jsonify({"success": True, "profile": parsed, "extracted_chars": len(resume_text)})
    except Exception as e:
        log.error("Parse error: {}".format(e))
        return jsonify({"error": str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate_content():
    body = request.get_json() or {}
    content_type = body.get("type", "cover")
    job = body.get("job", {})
    profile = body.get("profile", {})
    try:
        if content_type == "cover":
            prompt = "Write a professional 3-paragraph cover letter. Job: {} at {}, {}. Description: {}. Candidate: {}, {} years, Skills: {}. Tone: professional, warm, English only.".format(
                job.get("title", ""), job.get("company", ""), job.get("location", ""),
                job.get("description", "")[:300], profile.get("title", ""),
                profile.get("experience_years", 0), ", ".join(profile.get("skills", [])[:8])
            )
            result = call_claude(prompt, "Expert cover letter writer.", 800)
        else:
            prompt = "Tailor resume for this job. Job: {} at {}. Description: {}. Candidate: {}, {} yrs, Skills: {}. Output: 1) Summary 2) Top 8 skills 3) Rewritten bullets.".format(
                job.get("title", ""), job.get("company", ""),
                job.get("description", "")[:300], profile.get("title", ""),
                profile.get("experience_years", 0), ", ".join(profile.get("skills", [])[:10])
            )
            result = call_claude(prompt, "Expert resume writer.", 1200)
        return jsonify({"success": True, "content": result})
    except Exception as e:
        log.error("Generate error: {}".format(e))
        return jsonify({"error": str(e)}), 500

@app.route("/search", methods=["POST"])
def search_jobs():
    b = request.get_json() or {}
    kw = b.get("keywords", "senior engineer europe")
    prof = b.get("profile", {})
    sources = b.get("sources", ["arbeitnow", "remotive", "adzuna", "weworkremotely", "themuse"])
    aid = b.get("adzuna_app_id", "") or os.environ.get("ADZUNA_APP_ID", "")
    akey = b.get("adzuna_app_key", "") or os.environ.get("ADZUNA_APP_KEY", "")
    profile_title = (prof.get("title") or "").strip()
    fallback_kw = profile_title or kw
    tasks = []
    if "arbeitnow" in sources:
        tasks.append(lambda k=kw: fetch_arbeitnow(k))
        tasks.append(lambda k=fallback_kw: fetch_arbeitnow(k))
    if "remotive" in sources:
        tasks.append(lambda k=kw: fetch_remotive(k))
    if "weworkremotely" in sources:
        tasks.append(lambda k=kw: fetch_weworkremotely(k))
    if "themuse" in sources:
        tasks.append(lambda k=kw: fetch_themuse(k))
    if "stepstone" in sources:
        tasks.append(lambda k=kw: fetch_stepstone(k))
    if "naukri" in sources:
        tasks.append(lambda k=kw: fetch_naukri(k))
    if "iimjobs" in sources:
        tasks.append(lambda k=kw: fetch_iimjobs(k))
    if "instahyre" in sources:
        tasks.append(lambda k=kw: fetch_instahyre(k))
    if "adzuna" in sources and aid and akey:
        tasks.append(lambda k=kw: fetch_adzuna(k, aid, akey))
        if profile_title:
            tasks.append(lambda k=profile_title: fetch_adzuna(k, aid, akey))
    all_jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(t) for t in tasks]
        for fut in as_completed(futures, timeout=25):
