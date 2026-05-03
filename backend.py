"""
JobAgent EU - Backend for Railway deployment
Supports PORT env var, CORS for Vercel
"""
import time, re, logging, os
from datetime import datetime
from urllib.parse import quote_plus
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=["http://localhost:3000","https://*.vercel.app","https://*.railway.app","*"])

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}

def scrape_eurotechjobs(keywords):
    results = []
    try:
        session = requests.Session(); session.headers.update(HEADERS)
        session.get("https://www.eurotechjobs.com", timeout=10); time.sleep(1)
        r = session.get(f"https://www.eurotechjobs.com/job_search/all/all/1/all/all/all/all/all/{quote_plus(keywords)}", timeout=15)
        if r.status_code != 200: return []
        soup = BeautifulSoup(r.text, "lxml")
        for card in soup.select("div.job_listing,li.job_listing,.job-card,article.job")[:20]:
            t = card.select_one("h2 a,h3 a,.job-title a"); l = card.select_one("a[href]")
            if not t: continue
            href = l["href"] if l else ""
            results.append({"id":f"etj_{len(results)}","title":t.get_text(strip=True),"company":(card.select_one(".company,strong") or t).get_text(strip=True),"location":(card.select_one(".location") or type('',(),{'get_text':lambda **k:"Europe"})()).get_text(strip=True),"salary":"Competitive","url":("https://www.eurotechjobs.com"+href if href.startswith("/") else href),"source":"EuroTechJobs","tags":extract_tags(card.get_text()),"posted":extract_date(card.get_text()),"description":card.get_text(strip=True)[:400],"match":0})
    except Exception as e: log.error(f"ETJ: {e}")
    return results

def fetch_arbeitnow(keywords):
    results = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        kw = keywords.lower().split()
        for job in r.json().get("data",[]):
            if any(k in f"{job.get('title','')} {job.get('description','')}".lower() for k in kw):
                results.append({"id":f"arb_{job.get('slug',len(results))}","title":job.get("title",""),"company":job.get("company_name",""),"location":job.get("location","Europe"),"salary":"Competitive","url":job.get("url","https://www.arbeitnow.com"),"source":"Arbeitnow","tags":job.get("tags",[])[:4]+(["Visa Sponsor"] if job.get("visa_sponsored") else []),"posted":"Recently","description":job.get("description","")[:400],"match":0})
    except Exception as e: log.error(f"Arbeitnow: {e}")
    return results

def fetch_remotive(keywords):
    results = []
    try:
        r = requests.get(f"https://remotive.com/api/remote-jobs?search={quote_plus(keywords)}&limit=20", headers=HEADERS, timeout=15)
        if r.status_code != 200: return []
        for job in r.json().get("jobs",[]):
            results.append({"id":f"rem_{job.get('id',len(results))}","title":job.get("title",""),"company":job.get("company_name",""),"location":job.get("candidate_required_location","Remote/Europe"),"salary":job.get("salary","Competitive"),"url":job.get("url","https://remotive.com"),"source":"Remotive","tags":job.get("tags",[])[:4]+["Remote","English Only"],"posted":job.get("publication_date","")[:10],"description":BeautifulSoup(job.get("description",""),"lxml").get_text()[:400],"match":0})
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
                results.append({"id":f"adz_{job.get('id',len(results))}","title":job.get("title",""),"company":job.get("company",{}).get("display_name",""),"location":job.get("location",{}).get("display_name",""),"salary":f"€{int(job.get('salary_min',0)):,}–€{int(job.get('salary_max',0)):,}" if job.get("salary_min") else "Competitive","url":job.get("redirect_url",""),"source":"Adzuna","tags":["English Friendly"],"posted":job.get("created","")[:10],"description":job.get("description","")[:400],"match":0})
    except Exception as e: log.error(f"Adzuna: {e}")
    return results

def extract_tags(text):
    return [t for t in ["Cloudera","Hadoop","Spark","Hive","HDFS","Kafka","CDP","CDH","Python","Linux","English","Visa","Relocation","Big Data"] if t.lower() in text.lower()][:5]

def extract_date(text):
    m = re.search(r"(\d+\s*(day|hour|week)[s]?\s*ago)", text, re.IGNORECASE)
    return m.group(0) if m else "Recently"

def score_job(job, profile):
    score = 50
    skills = [s.lower() for s in profile.get("skills",[])]
    title = job["title"].lower(); body = (job.get("description","")+" ".join(job.get("tags",[]))).lower()
    for kw in ["support","engineer","data","platform","cloudera","hadoop","senior"]:
        if kw in title: score += 5
    for s in skills:
        if s in body or s in title: score += 4
    if any(t in ["Visa Sponsor","Relocation Package"] for t in job.get("tags",[])): score += 8
    if any(t in ["English Only","English Friendly"] for t in job.get("tags",[])): score += 6
    return min(score, 99)

def dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        k = f"{j['title'].lower()}|{j['company'].lower()}"
        if k not in seen: seen.add(k); out.append(j)
    return out

@app.route("/health")
def health(): return jsonify({"status":"ok","ts":datetime.now().isoformat()})

@app.route("/search", methods=["POST"])
def search():
    b = request.get_json() or {}
    kw = b.get("keywords","cloudera support engineer"); prof = b.get("profile",{}); sources = b.get("sources",["arbeitnow","remotive"])
    jobs = []
    if "eurotechjobs" in sources: jobs += scrape_eurotechjobs(kw)
    if "arbeitnow" in sources: jobs += fetch_arbeitnow(kw)
    if "remotive" in sources: jobs += fetch_remotive(kw)
    if "adzuna" in sources:
    jobs += fetch_adzuna(
        kw,
        b.get("adzuna_app_id","") or os.environ.get("ADZUNA_APP_ID",""),
        b.get("adzuna_app_key","") or os.environ.get("ADZUNA_APP_KEY","")
    )
    jobs = dedup(jobs)
    for j in jobs: j["match"] = score_job(j, prof)
    jobs.sort(key=lambda j: j["match"], reverse=True)
    return jsonify({"jobs":jobs,"total":len(jobs)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*45}\n  JobAgent EU Backend — port {port}\n{'='*45}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
