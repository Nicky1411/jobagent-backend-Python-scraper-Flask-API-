"""
JobAgent EU - Daily Scheduler
==============================
Runs the full agent loop on a schedule and sends email digest.
Triggered by Railway Cron or manually via POST /scheduler/run

Setup:
1. Add SENDGRID_API_KEY to Railway env vars (free at sendgrid.com)
2. Add RECIPIENT_EMAIL to Railway env vars (her email)
3. Add SENDER_EMAIL to Railway env vars (verified sender in SendGrid)
4. Add STORED_PROFILE to Railway env vars (JSON of her parsed profile)
5. Add TARGET_COUNTRIES to Railway env vars (e.g. "Netherlands,Germany,India")
6. Set Railway Cron to: 0 8 * * * (8 AM daily)
"""

import json
import logging
import os
import base64
from datetime import datetime

import requests

log = logging.getLogger(__name__)

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
STORED_PROFILE = os.environ.get("STORED_PROFILE", "")
TARGET_COUNTRIES = os.environ.get("TARGET_COUNTRIES", "Netherlands,Germany")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:5000")

# Geography → sources mapping
GEOGRAPHY_BOARDS = {
    "Netherlands":      ["arbeitnow", "remotive", "themuse", "adzuna"],
    "Germany":          ["stepstone", "arbeitnow", "themuse", "adzuna"],
    "Sweden":           ["arbeitnow", "remotive", "themuse", "adzuna"],
    "UK":               ["remotive", "themuse", "weworkremotely", "adzuna"],
    "India":            ["naukri", "iimjobs", "instahyre", "adzuna"],
    "Singapore":        ["remotive", "themuse", "adzuna"],
    "Remote/Worldwide": ["remotive", "weworkremotely", "themuse", "arbeitnow"],
}

ADZUNA_COUNTRIES = {
    "Netherlands": "nl", "Germany": "de", "Sweden": "se",
    "UK": "gb", "India": "in", "Singapore": "sg",
    "Austria": "at", "Switzerland": "ch",
}


def get_sources_for_countries(countries):
    sources = set()
    for c in countries:
        for s in GEOGRAPHY_BOARDS.get(c, ["arbeitnow", "remotive"]):
            sources.add(s)
    return list(sources)


def get_adzuna_countries(countries):
    return [ADZUNA_COUNTRIES[c] for c in countries if c in ADZUNA_COUNTRIES]


def load_profile():
    """Load stored profile from env var."""
    if not STORED_PROFILE:
        raise Exception("STORED_PROFILE env var not set. Save her profile first via the app.")
    try:
        return json.loads(STORED_PROFILE)
    except json.JSONDecodeError as e:
        raise Exception("STORED_PROFILE is not valid JSON: {}".format(e))


def get_recipient_email(profile):
    """Get recipient email — from env var, profile notify_email, or profile email."""
    if RECIPIENT_EMAIL:
        return RECIPIENT_EMAIL
    if profile.get("notify_email"):
        return profile["notify_email"]
    if profile.get("email"):
        return profile["email"]
    raise Exception(
        "No recipient email found. Set RECIPIENT_EMAIL env var in Railway, "
        "or use the 'Enable Daily Email' button in the app."
    )


def run_agent_search(profile, countries):
    """Call the backend agent/run endpoint."""
    sources = get_sources_for_countries(countries)
    adzuna_countries = get_adzuna_countries(countries)

    title_words = " ".join((profile.get("title") or "").lower().split()[:3])
    top_skills = " ".join(profile.get("skills", [])[:2]).lower()
    keywords = "{} {}".format(title_words, top_skills).strip()

    log.info("Scheduler: running agent for {} with keywords='{}'".format(
        profile.get("name"), keywords
    ))

    r = requests.post(
        "{}/agent/run".format(BACKEND_URL),
        json={
            "profile": profile,
            "state": {},
            "keywords": keywords,
            "sources": sources,
            "adzuna_countries": adzuna_countries,
            "adzuna_app_id": os.environ.get("ADZUNA_APP_ID", ""),
            "adzuna_app_key": os.environ.get("ADZUNA_APP_KEY", ""),
            "target_countries": countries,
        },
        timeout=300,
    )
    if r.status_code != 200:
        raise Exception("Agent run failed: {} {}".format(r.status_code, r.text[:200]))
    return r.json()


def generate_resume_for_job(job, profile):
    """Generate tailored resume for a specific job."""
    try:
        r = requests.post(
            "{}/generate".format(BACKEND_URL),
            json={"type": "resume", "job": job, "profile": profile},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("content", "")
    except Exception as e:
        log.error("Resume generation failed: {}".format(e))
    return ""


def generate_cover_for_job(job, profile):
    """Generate cover letter for a specific job."""
    try:
        r = requests.post(
            "{}/generate".format(BACKEND_URL),
            json={"type": "cover", "job": job, "profile": profile},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("content", "")
    except Exception as e:
        log.error("Cover letter generation failed: {}".format(e))
    return ""


def build_email_html(profile, agent_result, job_applications):
    """Build beautiful HTML email digest."""
    name = profile.get("name", "there")
    date_str = datetime.now().strftime("%A, %d %B %Y")
    applied_count = agent_result.get("applied_count", 0)
    pending = agent_result.get("pending_approval", [])
    followups = agent_result.get("followups_drafted", [])

    # Job cards HTML
    job_cards = ""
    for item in job_applications[:5]:
        job = item["job"]
        match = job.get("match", 0)
        match_color = "#22c55e" if match >= 85 else "#3b82f6" if match >= 70 else "#f59e0b"
        reason = job.get("match_reason", "")
        action_badge = (
            '<span style="background:#22c55e20;color:#22c55e;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:600;">⚡ Auto-Applied</span>'
            if item.get("auto_applied") else
            '<span style="background:#f59e0b20;color:#f59e0b;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:600;">👀 Needs Your Approval</span>'
        )
        job_url = job.get("url", "#")
        job_cards += """
        <div style="background:#0f1929;border:1px solid #1c2a3a;border-radius:12px;padding:20px;margin-bottom:16px;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
                <div>
                    <div style="font-size:16px;font-weight:700;color:#e2e8f0;margin-bottom:3px;">{title}</div>
                    <div style="font-size:13px;color:#64748b;">{company} · {location}</div>
                </div>
                <div style="text-align:right;">
                    <div style="font-size:22px;font-weight:800;color:{match_color};">{match}%</div>
                    <div style="font-size:10px;color:#64748b;">match</div>
                </div>
            </div>
            {reason_html}
            <div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap;">
                {action_badge}
                <a href="{url}" style="background:#3b82f620;color:#3b82f6;padding:4px 12px;border-radius:5px;font-size:12px;text-decoration:none;border:1px solid #3b82f640;">🔗 View Job</a>
            </div>
        </div>
        """.format(
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            match=match,
            match_color=match_color,
            reason_html='<div style="font-size:12px;color:#94a3b8;font-style:italic;margin-bottom:8px;">🤖 {}</div>'.format(reason) if reason else "",
            action_badge=action_badge,
            url=job_url,
        )

    # Followup section
    followup_section = ""
    if followups:
        followup_items = "".join([
            '<div style="background:#06b6d420;border:1px solid #06b6d440;border-radius:8px;padding:12px;margin-bottom:8px;">'
            '<div style="font-weight:600;color:#e2e8f0;margin-bottom:4px;">{} — {}</div>'
            '<div style="font-size:12px;color:#94a3b8;">{} days since application</div>'
            '</div>'.format(f["title"], f["company"], f["days_since_applied"])
            for f in followups
        ])
        followup_section = """
        <div style="margin-bottom:28px;">
            <h2 style="color:#06b6d4;font-size:16px;margin-bottom:12px;">✉️ Follow-up Emails Drafted</h2>
            {}
            <p style="font-size:12px;color:#64748b;">Check the JobAgent app to copy and send these follow-ups.</p>
        </div>
        """.format(followup_items)

    html = """
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body style="margin:0;padding:0;background:#07090f;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <div style="max-width:600px;margin:0 auto;padding:32px 20px;">

            <!-- Header -->
            <div style="text-align:center;margin-bottom:32px;">
                <div style="font-size:28px;font-weight:900;color:#e2e8f0;">⚡ JobAgent <span style="color:#3b82f6;">EU</span></div>
                <div style="color:#64748b;font-size:14px;margin-top:4px;">Daily Job Digest · {date}</div>
            </div>

            <!-- Summary banner -->
            <div style="background:linear-gradient(135deg,#3b82f620,#8b5cf620);border:1px solid #3b82f640;border-radius:14px;padding:20px;margin-bottom:28px;text-align:center;">
                <div style="font-size:15px;color:#e2e8f0;margin-bottom:12px;">Good morning, <strong>{name}</strong>! Here's your daily job search update.</div>
                <div style="display:flex;justify-content:center;gap:28px;flex-wrap:wrap;">
                    <div><div style="font-size:28px;font-weight:800;color:#22c55e;">{applied}</div><div style="font-size:11px;color:#64748b;">Auto-Applied</div></div>
                    <div><div style="font-size:28px;font-weight:800;color:#f59e0b;">{pending}</div><div style="font-size:11px;color:#64748b;">Need Approval</div></div>
                    <div><div style="font-size:28px;font-weight:800;color:#06b6d4;">{followup_count}</div><div style="font-size:11px;color:#64748b;">Follow-ups</div></div>
                </div>
            </div>

            <!-- Job cards -->
            <h2 style="color:#e2e8f0;font-size:16px;margin-bottom:16px;">🎯 Today's Top Matches</h2>
            {job_cards}

            {followup_section}

            <!-- CTA -->
            <div style="text-align:center;margin-top:28px;">
                <a href="https://jobagent-frontend-react-web-app.vercel.app" 
                   style="background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;padding:14px 32px;border-radius:10px;text-decoration:none;font-weight:700;font-size:15px;display:inline-block;">
                    Open JobAgent →
                </a>
            </div>

            <!-- Footer -->
            <div style="text-align:center;margin-top:32px;color:#374151;font-size:12px;">
                Tailored resumes attached as .txt files · Powered by Claude AI
            </div>
        </div>
    </body>
    </html>
    """.format(
        date=date_str,
        name=name,
        applied=applied_count,
        pending=len(pending),
        followup_count=len(followups),
        job_cards=job_cards,
        followup_section=followup_section,
    )
    return html


def send_email_digest(profile, agent_result, job_applications):
    """Send email via SendGrid with job digest and resume attachments."""
    if not SENDGRID_API_KEY:
        raise Exception("SENDGRID_API_KEY not set in Railway env vars")
    if not SENDER_EMAIL:
        raise Exception("SENDER_EMAIL not set in Railway env vars")
    recipient = get_recipient_email(profile)

    name = profile.get("name", "Candidate")
    date_str = datetime.now().strftime("%d %B %Y")
    applied = agent_result.get("applied_count", 0)
    pending = len(agent_result.get("pending_approval", []))

    # Build HTML
    html_content = build_email_html(profile, agent_result, job_applications)

    # Build attachments - tailored resumes
    attachments = []
    for i, item in enumerate(job_applications[:3]):  # attach top 3 resumes
        resume = item.get("resume", "")
        if resume:
            job = item["job"]
            filename = "Resume_{}_{}.txt".format(
                name.replace(" ", "_"),
                job.get("company", "Company").replace(" ", "_")
            )
            attachments.append({
                "content": base64.b64encode(resume.encode()).decode(),
                "filename": filename,
                "type": "text/plain",
                "disposition": "attachment",
            })

    # SendGrid API call
    payload = {
        "personalizations": [{"to": [{"email": recipient, "name": name}]}],
        "from": {"email": SENDER_EMAIL, "name": "JobAgent EU"},
        "subject": "⚡ {date} — {applied} applied, {pending} need your approval".format(
            date=date_str, applied=applied, pending=pending
        ),
        "content": [{"type": "text/html", "value": html_content}],
    }
    if attachments:
        payload["attachments"] = attachments

    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={
            "Authorization": "Bearer {}".format(SENDGRID_API_KEY),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    if r.status_code not in [200, 202]:
        raise Exception("SendGrid error {}: {}".format(r.status_code, r.text[:200]))

    log.info("Email sent to {} via SendGrid".format(recipient))
    return True


def run_scheduled_job():
    """
    Main scheduled job — runs at 8 AM daily.
    1. Load stored profile
    2. Run agent search
    3. Generate resumes for top matches
    4. Send email digest
    """
    log.info("=== Scheduled job started at {} ===".format(datetime.now().isoformat()))

    # 1. Load profile
    profile = load_profile()
    log.info("Profile loaded: {}".format(profile.get("name")))

    # 2. Parse target countries
    countries = [c.strip() for c in TARGET_COUNTRIES.split(",") if c.strip()]
    log.info("Target countries: {}".format(countries))

    # 3. Run agent
    agent_result = run_agent_search(profile, countries)
    applied_jobs = agent_result.get("applied_jobs", [])
    pending = agent_result.get("pending_approval", [])
    all_jobs_state = agent_result.get("state", {}).get("jobs", {})

    log.info("Agent complete: {} applied, {} pending".format(
        len(applied_jobs), len(pending)
    ))

    # 4. Generate resumes for top 5 jobs (applied + pending)
    top_job_ids = [a["job_id"] for a in applied_jobs[:3]] + \
                  [p["job_id"] for p in pending[:2]]

    job_applications = []
    for job_id in top_job_ids:
        job = all_jobs_state.get(job_id, {})
        if not job:
            continue
        log.info("Generating resume for: {}".format(job.get("title", "")))
        resume = generate_resume_for_job(job, profile)
        cover = generate_cover_for_job(job, profile)
        job_applications.append({
            "job": job,
            "resume": resume,
            "cover": cover,
            "auto_applied": job_id in [a["job_id"] for a in applied_jobs],
        })

    # 5. Send email
    if job_applications or agent_result.get("followups_drafted"):
        send_email_digest(profile, agent_result, job_applications)
        log.info("Email digest sent!")
    else:
        log.info("No jobs found today — skipping email")

    return {
        "status": "complete",
        "applied": len(applied_jobs),
        "pending": len(pending),
        "emails_with_resumes": len(job_applications),
        "email_sent": bool(job_applications),
    }
