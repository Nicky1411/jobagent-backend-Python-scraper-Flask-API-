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

# Import all job board scrapers
from scrapers import (
    fetch_arbeitnow, fetch_remotive, fetch_weworkremotely,
    fetch_themuse, fetch_adzuna, fetch_stepstone,
    fetch_naukri, fetch_iimjobs, fetch_instahyre
)

def score_job(job, profile):
    score = 30
    skills = [s.lower() for s in profile.get("skills", [])]
    profile_title = (profile.get("title") or "").lower()
    title = (job.get("title") or "").lower()
    tags_str = " ".join(t for t in job.get("tags", []) if t)
    combined = title + " " + ((job.get("description") or "") + " " + tags_str).lower()
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
    location = (job.get("location") or "").lower()
    if any(loc in location for loc in ["netherlands", "germany", "amsterdam", "berlin",
                                        "munich", "hamburg", "stockholm", "vienna",
                                        "zurich", "europe", "remote", "worldwide",
                                        "india", "bangalore", "mumbai", "delhi",
                                        "hyderabad", "pune", "chennai", "bengaluru",
                                        "gurgaon", "noida", "kolkata", "ahmedabad"]):
        score += 8
    if any(t in ["Visa Sponsor", "Relocation Package"] for t in job.get("tags", [])):
        score += 12
    if any(t in ["English Only", "English Friendly"] for t in job.get("tags", [])):
        score += 10
    return int(max(0, min(score, 99)))

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
                job["match"] = int(max(0, min(int(s.get("score") or job.get("match") or 50), 99)))
                job["match_reason"] = s.get("reason", "")
                job["match_highlight"] = s.get("highlight", "")
        log.info("Claude scored {} jobs".format(len(to_score)))
    except Exception as e:
        log.error("Claude scoring error: {}".format(e))
    return to_score + rest

def dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        title = (j.get("title") or "").lower().strip()
        company = (j.get("company") or "").lower().strip()
        if not title:
            continue  # skip jobs with no title
        k = "{}|{}".format(title, company)
        if k not in seen:
            seen.add(k)
            # Ensure match is always an int
            j["match"] = int(j.get("match") or 0)
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
    # For Indian boards, use shorter role-focused keywords
    india_kw = " ".join(kw.split()[:3])  # e.g. "staff premier support"
    if "naukri" in sources:
        tasks.append(lambda k=india_kw: fetch_naukri(k))
        tasks.append(lambda k=profile_title: fetch_naukri(k))  # also try exact title
    if "iimjobs" in sources:
        tasks.append(lambda k=india_kw: fetch_iimjobs(k))
    if "instahyre" in sources:
        tasks.append(lambda k=india_kw: fetch_instahyre(k))
    if "adzuna" in sources and aid and akey:
        adzuna_ctries = b.get("adzuna_countries", ["nl", "de", "gb"])
        if not adzuna_ctries: adzuna_ctries = ["nl", "de", "gb"]
        tasks.append(lambda k=kw, c=adzuna_ctries: fetch_adzuna(k, aid, akey, c))
        if profile_title:
            tasks.append(lambda k=profile_title, c=adzuna_ctries: fetch_adzuna(k, aid, akey, c))
    all_jobs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(t) for t in tasks]
        for fut in as_completed(futures, timeout=25):
            try:
                all_jobs += fut.result(timeout=5)
            except Exception:
                pass
    all_jobs = dedup(all_jobs)
    for j in all_jobs:
        j["match"] = score_job(j, prof)
    all_jobs.sort(key=lambda j: j.get("match") or 0, reverse=True)
    if prof.get("name"):
        all_jobs = claude_score_jobs(all_jobs, prof)
        all_jobs.sort(key=lambda j: j.get("match") or 0, reverse=True)
    log.info("Search returned {} jobs".format(len(all_jobs)))
    return jsonify({"jobs": all_jobs, "total": len(all_jobs)})

@app.route("/agent/run", methods=["POST"])
def run_agent():
    try:
        from agent import JobAgent
    except ImportError as ie:
        log.error("Cannot import agent: {}".format(ie))
        return jsonify({"error": "agent.py not found. Upload it to the backend repo."}), 500
    b = request.get_json() or {}
    profile = b.get("profile", {})
    saved_state = b.get("state", {})
    sources = b.get("sources", ["arbeitnow", "remotive", "weworkremotely", "themuse", "adzuna"])
    aid = b.get("adzuna_app_id", "") or os.environ.get("ADZUNA_APP_ID", "")
    akey = b.get("adzuna_app_key", "") or os.environ.get("ADZUNA_APP_KEY", "")
    if not profile.get("name"):
        return jsonify({"error": "Profile required. Parse resume first."}), 400

    def search_fn(keywords, srcs):
        # Shorten keywords if too long (>5 words)
        kw_words = keywords.strip().split()
        if len(kw_words) > 5:
            keywords = " ".join(kw_words[:4])
        log.info("Agent search_fn: keywords='{}'".format(keywords))

        jobs = []
        tasks = []
        if "arbeitnow" in srcs:
            tasks.append(lambda k=keywords: fetch_arbeitnow(k))
        if "remotive" in srcs:
            tasks.append(lambda k=keywords: fetch_remotive(k))
        if "weworkremotely" in srcs:
            tasks.append(lambda k=keywords: fetch_weworkremotely(k))
        if "themuse" in srcs:
            tasks.append(lambda k=keywords: fetch_themuse(k))
        if "adzuna" in srcs and aid and akey:
            agt_ctries = b.get("adzuna_countries", ["nl", "de", "gb"]) or ["nl","de","gb"]
            tasks.append(lambda k=keywords, c=agt_ctries: fetch_adzuna(k, aid, akey, c))
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(t) for t in tasks]
            for fut in as_completed(futs, timeout=20):
                try:
                    jobs += fut.result(timeout=5)
                except Exception:
                    pass

        # If no results, try broader fallback keywords
        if len(jobs) < 3:
            log.info("Agent: 0 results, trying broader keywords")
            broad = " ".join(kw_words[:2]) if len(kw_words) >= 2 else "senior engineer"
            fallback_tasks = [
                lambda k=broad: fetch_arbeitnow(k),
                lambda k=broad: fetch_remotive(k),
                lambda k=broad: fetch_weworkremotely(k),
                lambda k=broad: fetch_themuse(k),
            ]
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = [ex.submit(t) for t in fallback_tasks]
                for fut in as_completed(futs, timeout=20):
                    try:
                        jobs += fut.result(timeout=5)
                    except Exception:
                        pass

        jobs = dedup(jobs)
        for j in jobs:
            j["match"] = score_job(j, profile)
        jobs.sort(key=lambda j: j.get("match") or 0, reverse=True)
        if jobs:
            jobs = claude_score_jobs(jobs, profile)
            jobs.sort(key=lambda j: j.get("match") or 0, reverse=True)
        log.info("Agent search_fn returned {} jobs".format(len(jobs)))
        return jobs

    def generate_fn(content_type, job, prof):
        try:
            if content_type == "cover":
                prompt = "Write a 3-paragraph cover letter. Job: {} at {}, {}. Description: {}. Candidate: {}, {} yrs, Skills: {}.".format(
                    job.get("title",""), job.get("company",""), job.get("location",""),
                    job.get("description","")[:300], prof.get("title",""),
                    prof.get("experience_years",0), ", ".join(prof.get("skills",[])[:8])
                )
                return call_claude(prompt, "Expert cover letter writer.", 600)
            else:
                prompt = "Tailor resume. Job: {} at {}. Description: {}. Candidate: {}, Skills: {}. Output: Summary + Skills + Bullets.".format(
                    job.get("title",""), job.get("company",""),
                    job.get("description","")[:200], prof.get("title",""),
                    ", ".join(prof.get("skills",[])[:10])
                )
                return call_claude(prompt, "Expert resume writer.", 800)
        except Exception as e:
            return "Error: {}".format(e)

    try:
        agent = JobAgent(call_claude, search_fn, generate_fn)
        result = agent.run(profile, saved_state)

        # Trim job descriptions from state to keep response small
        if "state" in result and "jobs" in result["state"]:
            for job in result["state"]["jobs"].values():
                job.pop("description", None)  # remove large description field

        log.info("Agent response: {} applied, {} pending, {} followups".format(
            result.get("applied_count", 0),
            len(result.get("pending_approval", [])),
            len(result.get("followups_drafted", []))
        ))
        return jsonify(result)
    except Exception as e:
        log.error("Agent error: {}".format(e))
        import traceback
        log.error(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/agent/approve", methods=["POST"])
def approve_job():
    body = request.get_json() or {}
    return jsonify({"status": "ok", "job_id": body.get("job_id"), "approved": body.get("approved", True)})


@app.route("/fetch-job", methods=["POST"])
def fetch_job():
    """Fetch job description from a URL (LinkedIn, company sites, etc)."""
    body = request.get_json() or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return jsonify({"error": "Could not fetch URL: {}".format(r.status_code)}), 400
        soup = BeautifulSoup(r.text, "lxml")
        # Remove nav, footer, scripts
        for tag in soup.select("nav, footer, script, style, header, [class*='nav'], [class*='footer']"):
            tag.decompose()
        # Try common job description selectors
        desc_el = (
            soup.select_one(".job-description, .description__text, [class*='job-desc'], "
                          "#job-details, .jobDescriptionContent, [data-testid='job-description'], "
                          "article, main, .content")
        )
        description = desc_el.get_text(separator=" ", strip=True)[:3000] if desc_el else soup.get_text(separator=" ", strip=True)[:3000]
        # Extract title and company from meta/title tags
        title = ""
        company = ""
        og_title = soup.select_one('meta[property="og:title"]')
        page_title = soup.select_one("title")
        if og_title:
            title = og_title.get("content", "")
        elif page_title:
            title = page_title.get_text(strip=True)
        # Clean up title (often "Job Title | Company | LinkedIn")
        if " | " in title:
            parts = title.split(" | ")
            title = parts[0].strip()
            if len(parts) > 1:
                company = parts[1].strip()
        return jsonify({"description": description, "title": title, "company": company, "url": url})
    except Exception as e:
        log.error("fetch_job error: {}".format(e))
        return jsonify({"error": str(e)}), 500


@app.route("/scheduler/run", methods=["POST", "GET"])
def run_scheduler():
    """
    Triggered by Railway Cron or manually.
    Returns immediately, runs job in background thread.
    """
    import threading
    def run_in_background():
        try:
            from scheduler import run_scheduled_job
            result = run_scheduled_job()
            log.info("Scheduled job complete: {}".format(result))
        except Exception as e:
            log.error("Scheduler error: {}".format(e))
            import traceback
            log.error(traceback.format_exc())

    thread = threading.Thread(target=run_in_background, daemon=True)
    thread.start()
    return jsonify({
        "status": "started",
        "message": "Scheduler running in background. Check Railway logs for progress.",
        "logs_url": "https://railway.app — Deployments → Deploy Logs"
    })


@app.route("/profile/save", methods=["POST"])
def save_profile():
    """Save profile + email settings and return Railway setup instructions."""
    body = request.get_json() or {}
    profile = body.get("profile", {})
    recipient_email = body.get("recipient_email", "")
    schedule_time = body.get("schedule_time", "08:00")
    target_countries = body.get("target_countries", ["Netherlands", "Germany"])

    if not profile.get("name"):
        return jsonify({"error": "No profile provided"}), 400
    if not recipient_email:
        return jsonify({"error": "Email address required"}), 400

    profile_json = json.dumps(profile)
    countries_str = ",".join(target_countries) if isinstance(target_countries, list) else target_countries

    # Parse schedule time into cron expression
    try:
        hour, minute = schedule_time.split(":")
        cron_expr = "{} {} * * *".format(minute, hour)
    except Exception:
        cron_expr = "0 8 * * *"

    return jsonify({
        "success": True,
        "recipient_email": recipient_email,
        "schedule_time": schedule_time,
        "cron_expr": cron_expr,
        "profile_json": profile_json,
        "env_vars": {
            "STORED_PROFILE": profile_json,
            "RECIPIENT_EMAIL": recipient_email,
            "TARGET_COUNTRIES": countries_str,
        },
        "instructions": [
            "1. Sign up free at sendgrid.com → Settings → API Keys → Create Key",
            "2. Railway → your service → Variables → add these:",
            "   SENDGRID_API_KEY = your SendGrid key",
            "   SENDER_EMAIL = your verified SendGrid sender email",
            "   STORED_PROFILE = (copy from box below)",
            "   RECIPIENT_EMAIL = {} (already filled)".format(recipient_email),
            "   TARGET_COUNTRIES = {} (already filled)".format(countries_str),
            "3. Railway → Settings → Cron → Add: {}".format(cron_expr),
            "4. Test now: POST /scheduler/run"
        ]
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n{}\n  JobAgent EU Backend — port {}\n  Claude: {}\n  Sources: 6\n{}\n".format(
        "="*45, port, "OK" if ANTHROPIC_KEY else "MISSING KEY", "="*45
    ))
    app.run(host="0.0.0.0", port=port, debug=False)
