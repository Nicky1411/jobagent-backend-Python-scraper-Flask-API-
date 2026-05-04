"""
JobAgent EU - Backend with Claude API proxy
Handles: job scraping, Claude API calls (parse, cover letter, resume rewrite)
"""
import time, re, logging, os, json, tempfile
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

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

# ── Claude API Proxy ─────────────────────────────────
def call_claude(prompt, system="", max_tokens=1000):
    if not ANTHROPIC_KEY:
        raise Exception("ANTHROPIC_KEY not set in environment")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
            "system": system or "You are an expert career advisor for European tech expat roles.",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise Exception(f"Claude API error {r.status_code}: {r.text[:200]}")
    return r.json()["content"][0]["text"]

# ── Job Scrapers ──────────────────────────────────────
def fetch_arbeitnow(keywords):
    results = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        kw = keywords.lower().split()
        for job in r.json().get("data", []):
            if any(k in f"{job.get('title','')} {job.get('description','')}".lower() for k in kw):
                results.append({"id": f"arb_{job.get('slug', len(results))}", "title": job.get("title", ""), "company": job.get("company_name", ""), "location": job.get("location", "Europe"), "salary": "Competitive", "url": job.get("url", ""), "source": "Arbeitnow", "tags": job.get("tags", [])[:4] + (["Visa Sponsor"] if job.get("visa_sponsored") else []), "posted": "Recently", "description": job.get("description", "")[:400], "match": 0})
    except Exception as e:
        log.error(f"Arbeitnow: {e}")
    return results

def fetch_remotive(keywords):
    results = []
    try:
        r = requests.get(f"https://remotive.com/api/remote-jobs?search={quote_plus(keywords)}&limit=20", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        for job in r.json().get("jobs", []):
            results.append({"id": f"rem_{job.get('id', len(results))}", "title": job.get("title", ""), "company": job.get("company_name", ""), "location": job.get("candidate_required_location", "Remote/Europe"), "salary": job.get("salary", "Competitive"), "url": job.get("url", ""), "source": "Remotive", "tags": job.get("tags", [])[:4] + ["Remote", "English Only"], "posted": job.get("publication_date", "")[:10], "description": BeautifulSoup(job.get("description", ""), "lxml").get_text()[:400], "match": 0})
    except Exception as e:
        log.error(f"Remotive: {e}")
    return results

def fetch_adzuna(keywords, app_id="", app_key=""):
    if not app_id or not app_key: return []
    results = []
    try:
        for c in ["nl", "de"]:
            r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{c}/search/1?app_id={app_id}&app_key={app_key}&results_per_page=10&what={quote_plus(keywords)}&content-type=application/json", timeout=15)
            if r.status_code != 200: continue
            for job in r.json().get("results", []):
                results.append({"id": f"adz_{job.get('id', len(results))}", "title": job.get("title", ""), "company": job.get("company", {}).get("display_name", ""), "location": job.get("location", {}).get("display_name", ""), "salary": f"€{int(job.get('salary_min',0)):,}–€{int(job.get('salary_max',0)):,}" if job.get("salary_min") else "Competitive", "url": job.get("redirect_url", ""), "source": "Adzuna", "tags": ["English Friendly"], "posted": job.get("created", "")[:10], "description": job.get("description", "")[:400], "match": 0})
    except Exception as e:
        log.error(f"Adzuna: {e}")
    return results

def extract_tags(text):
    return [t for t in ["Cloudera","Hadoop","Spark","Hive","HDFS","Kafka","CDP","CDH","Python","Linux","English","Visa","Relocation","Big Data","AWS","Azure"] if t.lower() in text.lower()][:5]

def score_job(job, profile):
    score = 50
    skills = [s.lower() for s in profile.get("skills", [])]
    title = job["title"].lower()
    body = (job.get("description", "") + " ".join(job.get("tags", []))).lower()
    for kw in ["support", "engineer", "data", "platform", "cloudera", "hadoop", "senior"]:
        if kw in title: score += 5
    for s in skills:
        if s in body or s in title: score += 4
    if any(t in ["Visa Sponsor", "Relocation Package"] for t in job.get("tags", [])): score += 8
    if any(t in ["English Only", "English Friendly"] for t in job.get("tags", [])): score += 6
    return min(score, 99)

def dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        k = f"{j['title'].lower()}|{j['company'].lower()}"
        if k not in seen:
            seen.add(k); out.append(j)
    return out

# ── Routes ────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "claude": bool(ANTHROPIC_KEY), "ts": datetime.now().isoformat()})

def extract_text_from_file(file_bytes, filename):
    """Extract plain text from PDF or DOCX file bytes."""
    ext = filename.lower().split(".")[-1]
    text = ""
    if ext == "pdf":
        try:
            import fitz  # PyMuPDF
            with fitz.open(stream=file_bytes, filetype="pdf") as doc:
                text = "\n".join(page.get_text() for page in doc)
        except Exception as e:
            raise Exception(f"Could not read PDF: {e}")
    elif ext in ["docx", "doc"]:
        try:
            from docx import Document
            from io import BytesIO
            doc = Document(BytesIO(file_bytes))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            raise Exception(f"Could not read Word file: {e}")
    elif ext == "txt":
        text = file_bytes.decode("utf-8", errors="ignore")
    else:
        raise Exception(f"Unsupported file type: {ext}. Please upload PDF, DOCX, or TXT.")
    return text.strip()


@app.route("/parse", methods=["POST"])
def parse_resume():
    """Parse resume from text OR uploaded file using Claude API."""
    resume_text = ""

    # Handle file upload
    if "file" in request.files:
        f = request.files["file"]
        filename = f.filename or "resume"
        try:
            file_bytes = f.read()
            resume_text = extract_text_from_file(file_bytes, filename)
            log.info(f"Extracted {len(resume_text)} chars from {filename}")
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # Handle JSON text
    elif request.is_json:
        body = request.get_json() or {}
        resume_text = body.get("text", "").strip()

    if not resume_text or len(resume_text) < 20:
        return jsonify({"error": "Could not extract text from file. Please try a different format or paste the text manually."}), 400

    try:
        result = call_claude(
            f'''Extract info from this resume. Return ONLY a JSON object. Keep values concise.

{{"name":"","email":"","phone":"","title":"","summary":"one sentence","experience_years":0,"skills":[],"experience":[{{"company":"","role":"","duration":"","bullets":[]}}],"education":"","certifications":[],"languages":[]}}

RESUME:
{resume_text[:5000]}''',
            "Return ONLY the filled JSON object. No markdown. No explanation. Keep all string values concise.",
            1500
        )
        cleaned = result.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(cleaned)
        return jsonify({"success": True, "profile": parsed, "extracted_chars": len(resume_text)})
    except Exception as e:
        log.error(f"Parse error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate_content():
    """Generate cover letter or tailored resume using Claude."""
    body = request.get_json() or {}
    content_type = body.get("type", "cover")  # cover | resume
    job = body.get("job", {})
    profile = body.get("profile", {})
    try:
        if content_type == "cover":
            result = call_claude(
                f"""Write a professional 3-paragraph cover letter for:
Job: {job.get('title')} at {job.get('company')}, {job.get('location')}
Description: {job.get('description', '')[:300]}
Candidate: {profile.get('title')}, {profile.get('experience_years')} years, Skills: {', '.join(profile.get('skills', [])[:8])}
Tone: professional, warm. English only. End with availability to interview.""",
                "You are an expert cover letter writer for European tech expat roles.",
                800
            )
        else:
            result = call_claude(
                f"""Tailor this candidate's resume for the job. Output:
1) Professional Summary (3 sentences)
2) Top 8 tailored skills as bullet points
3) Rewritten experience bullets matching job keywords

Job: {job.get('title')} at {job.get('company')}
Description: {job.get('description', '')[:300]}
Candidate: {profile.get('title')}, {profile.get('experience_years')} yrs
Skills: {', '.join(profile.get('skills', [])[:10])}
Experience: {str(profile.get('experience', []))[:500]}""",
                "You are an expert resume writer for European tech expat roles.",
                1200
            )
        return jsonify({"success": True, "content": result})
    except Exception as e:
        log.error(f"Generate error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/search", methods=["POST"])
def search_jobs():
    b = request.get_json() or {}
    kw = b.get("keywords", "cloudera support engineer")
    prof = b.get("profile", {})
    sources = b.get("sources", ["arbeitnow", "remotive"])
    jobs = []
    if "arbeitnow" in sources: jobs += fetch_arbeitnow(kw)
    if "remotive" in sources: jobs += fetch_remotive(kw)
    if "adzuna" in sources:
        jobs += fetch_adzuna(kw, b.get("adzuna_app_id", "") or os.environ.get("ADZUNA_APP_ID", ""), b.get("adzuna_app_key", "") or os.environ.get("ADZUNA_APP_KEY", ""))
    jobs = dedup(jobs)
    for j in jobs: j["match"] = score_job(j, prof)
    jobs.sort(key=lambda j: j["match"], reverse=True)
    return jsonify({"jobs": jobs, "total": len(jobs)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*45}\n  JobAgent EU Backend — port {port}\n  Claude API: {'✓ configured' if ANTHROPIC_KEY else '✗ missing ANTHROPIC_KEY'}\n{'='*45}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
