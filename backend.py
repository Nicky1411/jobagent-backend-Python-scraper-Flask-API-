"""
JobAgent EU - Backend (clean, no duplicate routes)
"""
import time, re, logging, os, json
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
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept-Language": "en-US,en;q=0.9"}
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

def call_claude(prompt, system="", max_tokens=1000):
    if not ANTHROPIC_KEY:
        raise Exception("ANTHROPIC_KEY not set")
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-5", "max_tokens": max_tokens, "system": system or "You are an expert career advisor.", "messages": [{"role": "user", "content": prompt}]},
        timeout=60)
    if r.status_code != 200:
        raise Exception(f"Claude API error {r.status_code}: {r.text[:200]}")
    return r.json()["content"][0]["text"]

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
        raise Exception(f"Unsupported file type: {ext}")

def fetch_arbeitnow(keywords):
    results = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        broad = keywords.lower().split() + ["data", "engineer", "cloud", "infrastructure", "platform", "support", "hadoop", "spark", "linux"]
        for job in r.json().get("data", []):
            if any(k in f"{job.get('title','')} {job.get('description','')}".lower() for k in broad):
                results.append({"id": f"arb_{job.get('slug',len(results))}", "title": job.get("title",""), "company": job.get("company_name",""), "location": job.get("location","Europe"), "salary": "Competitive", "url": job.get("url",""), "source": "Arbeitnow", "tags": job.get("tags",[])[:4]+(["Visa Sponsor"] if job.get("visa_sponsored") else []), "posted": "Recently", "description": job.get("description","")[:400], "match": 0})
    except Exception as e: log.error(f"Arbeitnow: {e}")
    return results

def fetch_remotive(keywords):
    results = []
    try:
        r = requests.get(f"https://remotive.com/api/remote-jobs?search={quote_plus(keywords)}&limit=20", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        for job in r.json().get("jobs", []):
            results.append({"id": f"rem_{job.get('id',len(results))}", "title": job.get("title",""), "company": job.get("company_name",""), "location": job.get("candidate_required_location","Remote/Europe"), "salary": job.get("salary","Competitive"), "url": job.get("url",""), "source": "Remotive", "tags": job.get("tags",[])[:4]+["Remote","English Only"], "posted": job.get("publication_date","")[:10], "description": BeautifulSoup(job.get("description",""),"lxml").get_text()[:400], "match": 0})
    except Exception as e: log.error(f"Remotive: {e}")
    return results

def fetch_adzuna(keywords, app_id="", app_key=""):
    if not app_id or not app_key: return []
    results = []
    try:
        for c in ["nl","de"]:
            r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{c}/search/1?app_id={app_id}&app_key={app_key}&results_per_page=10&what={quote_plus(keywords)}&content-type=application/json", timeout=15)
            if r.status_code != 200: continue
            for job in r.json().get("results",[]):
                results.append({"id": f"adz_{job.get('id',len(results))}", "title": job.get("title",""), "company": job.get("company",{}).get("display_name",""), "location": job.get("location",{}).get("display_name",""), "salary": f"€{int(job.get('salary_min',0)):,}–€{int(job.get('salary_max',0)):,}" if job.get("salary_min") else "Competitive", "url": job.get("redirect_url",""), "source": "Adzuna", "tags": ["English Friendly"], "posted": job.get("created","")[:10], "description": job.get("description","")[:400], "match": 0})
    except Exception as e: log.error(f"Adzuna: {e}")
    return results

def score_job(job, profile):
    """Generic relevance scoring — works for any profession."""
    score = 30
    skills = [s.lower() for s in profile.get("skills", [])]
    profile_title = (profile.get("title") or "").lower()
    title = job["title"].lower()
    combined = title + " " + (job.get("description","") + " ".join(job.get("tags",[]))).lower()

    # Skill overlap — core signal, works for any profession
    matched = sum(1 for s in skills if len(s) > 3 and s.lower() in combined)
    score += min(matched * 6, 40)

    # Title word overlap with profile title
    title_words = set(title.split())
    profile_words = set(profile_title.split())
    score += len(title_words & profile_words) * 5

    # Seniority match
    for level in ["senior","lead","principal","staff","head","director","manager"]:
        if level in profile_title and level in title: score += 8; break
        elif level in profile_title and level not in title: score -= 3

    # Experience years bonus
    exp = profile.get("experience_years", 0)
    if exp >= 5 and any(w in title for w in ["junior","intern","graduate","entry"]): score -= 20
    elif exp >= 4: score += 4

    # Location bonus — expat friendly
    location = job.get("location","").lower()
    if any(loc in location for loc in ["netherlands","germany","amsterdam","berlin","munich","hamburg","stockholm","europe","remote","worldwide"]): score += 8

    # Visa/relocation bonus — critical for expats
    if any(t in ["Visa Sponsor","Relocation Package","Visa Sponsorship"] for t in job.get("tags",[])): score += 12
    if any(t in ["English Only","English Friendly","English OK"] for t in job.get("tags",[])): score += 10

    return max(0, min(score, 99))

def dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        k = f"{j['title'].lower()}|{j['company'].lower()}"
        if k not in seen: seen.add(k); out.append(j)
    return out

@app.route("/health")
def health():
    return jsonify({"status":"ok","claude":bool(ANTHROPIC_KEY),"ts":datetime.now().isoformat()})

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
        return jsonify({"error": "Could not extract text. Please try a different format or paste text manually."}), 400
    try:
        result = call_claude(
            f'Extract info from this resume. Return ONLY a JSON object. Keep values concise.\n\n{{"name":"","email":"","phone":"","title":"","summary":"one sentence","experience_years":0,"skills":[],"experience":[{{"company":"","role":"","duration":"","bullets":[]}}],"education":"","certifications":[],"languages":[]}}\n\nRESUME:\n{resume_text[:5000]}',
            "Return ONLY the filled JSON object. No markdown. No explanation. Keep all string values concise.", 1500)
        cleaned = result.replace("```json","").replace("```","").strip()
        parsed = json.loads(cleaned)
        return jsonify({"success":True,"profile":parsed,"extracted_chars":len(resume_text)})
    except Exception as e:
        log.error(f"Parse error: {e}")
        return jsonify({"error":str(e)}), 500

@app.route("/generate", methods=["POST"])
def generate_content():
    body = request.get_json() or {}
    content_type = body.get("type","cover")
    job = body.get("job",{}); profile = body.get("profile",{})
    try:
        if content_type == "cover":
            result = call_claude(f"Write a professional 3-paragraph cover letter for:\nJob: {job.get('title')} at {job.get('company')}, {job.get('location')}\nDescription: {job.get('description','')[:300]}\nCandidate: {profile.get('title')}, {profile.get('experience_years')} years, Skills: {', '.join(profile.get('skills',[])[:8])}\nTone: professional, warm. English only. End with availability to interview.", "You are an expert cover letter writer.", 800)
        else:
            result = call_claude(f"Tailor this resume for the job.\nJob: {job.get('title')} at {job.get('company')}\nDescription: {job.get('description','')[:300]}\nCandidate: {profile.get('title')}, {profile.get('experience_years')} yrs, Skills: {', '.join(profile.get('skills',[])[:10])}\nOutput: 1) Summary 2) Top 8 skills 3) Rewritten bullets", "You are an expert resume writer.", 1200)
        return jsonify({"success":True,"content":result})
    except Exception as e:
        log.error(f"Generate error: {e}")
        return jsonify({"error":str(e)}), 500

@app.route("/search", methods=["POST"])
def search_jobs():
    b = request.get_json() or {}
    kw = b.get("keywords","cloudera support engineer"); prof = b.get("profile",{})
    sources = b.get("sources",["arbeitnow","remotive"])
    aid = b.get("adzuna_app_id","") or os.environ.get("ADZUNA_APP_ID","")
    akey = b.get("adzuna_app_key","") or os.environ.get("ADZUNA_APP_KEY","")
    # Build fallback keywords from profile title if available
    profile_title = (prof.get("title") or "").strip()
    top_skills = " ".join(prof.get("skills", [])[:2])
    fallback_kw = f"{profile_title} {top_skills}".strip() or "senior engineer europe"

    jobs = []
    if "arbeitnow" in sources:
        jobs += fetch_arbeitnow(kw)
        if len(jobs) < 8: jobs += fetch_arbeitnow(fallback_kw)
    if "remotive" in sources:
        jobs += fetch_remotive(kw)
        if len(jobs) < 8: jobs += fetch_remotive(profile_title or "senior engineer")
    if "adzuna" in sources:
        jobs += fetch_adzuna(kw, aid, akey)
        if profile_title: jobs += fetch_adzuna(profile_title, aid, akey)
    jobs = dedup(jobs)
    for j in jobs: j["match"] = score_job(j, prof)
    jobs.sort(key=lambda j: j["match"], reverse=True)
    log.info(f"Search returned {len(jobs)} jobs")
    return jsonify({"jobs":jobs,"total":len(jobs)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print(f"\n{'='*45}\n  JobAgent EU Backend — port {port}\n  Claude: {'OK' if ANTHROPIC_KEY else 'MISSING KEY'}\n{'='*45}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
