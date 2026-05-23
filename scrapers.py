"""
JobAgent EU - Job Board Scrapers
All fetch_* functions for each job source
"""
import time, re, logging, os
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9"
}

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
    """Scrape Naukri.com using requests (no Playwright needed)."""
    results = []
    try:
        query = quote_plus(keywords)
        url = "https://www.naukri.com/jobapi/v3/search?noOfResults=20&urlType=search_by_keyword&searchType=adv&keyword={}&jobAge=7".format(query)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "appid": "109",
            "systemid": "109",
        }
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for job in data.get("jobDetails", [])[:20]:
                results.append({
                    "id": "naukri_{}".format(job.get("jobId", len(results))),
                    "title": job.get("title", ""),
                    "company": job.get("companyName", ""),
                    "location": ", ".join(job.get("placeholders", [{}])[1].get("label", "India").split(",")[:2]) if len(job.get("placeholders", [])) > 1 else "India",
                    "salary": job.get("placeholders", [{}])[2].get("label", "Competitive") if len(job.get("placeholders", [])) > 2 else "Competitive",
                    "url": "https://www.naukri.com{}".format(job.get("jdURL", "")),
                    "source": "Naukri 🇮🇳",
                    "tags": ["India", "English"],
                    "posted": "Recently",
                    "description": job.get("jobDescription", "")[:400],
                    "match": 0,
                })
        else:
            # Fallback: scrape search page
            r2 = requests.get(
                "https://www.naukri.com/{}-jobs".format(keywords.lower().replace(" ", "-")),
                headers=HEADERS, timeout=15
            )
            if r2.status_code == 200:
                soup = BeautifulSoup(r2.text, "lxml")
                for card in soup.select(".jobTuple, article.jobTuple")[:10]:
                    title_el = card.select_one("a.title, .jobTitle")
                    if not title_el:
                        continue
                    results.append({
                        "id": "naukri_{}".format(len(results)),
                        "title": title_el.get_text(strip=True),
                        "company": (card.select_one(".companyInfo a") or card.select_one(".comp-name") or title_el).get_text(strip=True),
                        "location": (card.select_one(".location") or title_el).get_text(strip=True),
                        "salary": "Competitive",
                        "url": title_el.get("href", "https://www.naukri.com"),
                        "source": "Naukri 🇮🇳",
                        "tags": ["India", "English"],
                        "posted": "Recently",
                        "description": card.get_text(strip=True)[:400],
                        "match": 0,
                    })
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
