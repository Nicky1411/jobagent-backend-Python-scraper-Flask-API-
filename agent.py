"""
JobAgent EU - True Agentic Core
================================
Claude decides what to do next based on current state.
Implements: Perceive → Reason → Act → Reflect loop
"""
import json, logging, os
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

# ── TOOL DEFINITIONS (what Claude can call) ───────────
TOOLS = [
    {
        "name": "search_jobs",
        "description": "Search job boards for relevant positions matching the candidate profile",
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "string", "description": "Search keywords based on candidate skills and title"},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "Job board sources to search"}
            },
            "required": ["keywords"]
        }
    },
    {
        "name": "evaluate_jobs",
        "description": "Use Claude to score and evaluate a list of jobs against candidate profile. Returns scored jobs with reasons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs of jobs to evaluate"}
            },
            "required": ["job_ids"]
        }
    },
    {
        "name": "prepare_application",
        "description": "Prepare a full job application: rewrite resume + generate cover letter tailored to the job",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "ID of the job to prepare application for"}
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "mark_applied",
        "description": "Mark a job as applied and save to application tracker",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "ID of the applied job"},
                "method": {"type": "string", "enum": ["auto", "manual"], "description": "How application was submitted"}
            },
            "required": ["job_id", "method"]
        }
    },
    {
        "name": "draft_followup",
        "description": "Draft a follow-up email for an application that was submitted 7+ days ago with no response",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "ID of the job to follow up on"},
                "days_since_applied": {"type": "integer", "description": "Number of days since application was submitted"}
            },
            "required": ["job_id", "days_since_applied"]
        }
    },
    {
        "name": "request_human_approval",
        "description": "Ask the human to review and approve/reject a medium-match job before applying",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "ID of job needing approval"},
                "reason": {"type": "string", "description": "Why this job needs human review"},
                "recommendation": {"type": "string", "enum": ["apply", "skip"], "description": "Agent recommendation"}
            },
            "required": ["job_id", "reason", "recommendation"]
        }
    },
    {
        "name": "finish",
        "description": "Signal that the agent has completed its current run. Provide a summary of actions taken.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Summary of what the agent did this run"},
                "next_run_recommendation": {"type": "string", "description": "When to run again and what to focus on"}
            },
            "required": ["summary"]
        }
    }
]


class JobAgent:
    """
    True agentic job search system.
    Claude reasons about state and decides which tools to call.
    """

    def __init__(self, call_claude_fn, search_jobs_fn, generate_fn):
        self.call_claude = call_claude_fn
        self.search_jobs_fn = search_jobs_fn
        self.generate_fn = generate_fn

        # Agent state — persists across tool calls in a single run
        self.state = {
            "jobs": {},          # job_id -> job data
            "applications": {},  # job_id -> {status, date, resume, cover_letter, followup}
            "pending_approval": [],  # jobs waiting for human review
            "followups_drafted": [],  # follow-up emails drafted
            "actions_taken": [],  # log of what happened
            "run_started": datetime.now().isoformat(),
        }

    def load_state(self, saved_state: dict):
        """Load previous state (applications, etc.) from storage."""
        if saved_state:
            self.state["applications"] = saved_state.get("applications", {})
            self.state["jobs"] = saved_state.get("jobs", {})

    def get_state_summary(self) -> str:
        """Build a compact state summary for Claude to reason about."""
        apps = self.state["applications"]
        total_applied = len([a for a in apps.values() if a.get("status") == "applied"])
        pending = len([a for a in apps.values() if a.get("status") == "pending_approval"])

        # Find applications needing follow-up (7+ days old, no response)
        needs_followup = []
        for jid, app in apps.items():
            if app.get("status") == "applied" and not app.get("followed_up"):
                applied_date = datetime.fromisoformat(app.get("date", datetime.now().isoformat()))
                days_ago = (datetime.now() - applied_date).days
                if days_ago >= 7:
                    job = self.state["jobs"].get(jid, {})
                    needs_followup.append({
                        "job_id": jid,
                        "title": job.get("title", "Unknown"),
                        "company": job.get("company", "Unknown"),
                        "days_ago": days_ago
                    })

        return json.dumps({
            "total_applied": total_applied,
            "pending_approval": pending,
            "applications_needing_followup": needs_followup,
            "jobs_in_memory": len(self.state["jobs"]),
        })

    # ── TOOL IMPLEMENTATIONS ──────────────────────────
    def tool_search_jobs(self, keywords: str, sources: list = None) -> dict:
        sources = sources or ["arbeitnow", "remotive", "weworkremotely", "themuse", "adzuna"]
        log.info(f"Agent: searching jobs with keywords='{keywords}'")
        results = self.search_jobs_fn(keywords, sources)
        # Store jobs in state
        for job in results:
            self.state["jobs"][job["id"]] = job
        return {
            "found": len(results),
            "job_ids": [j["id"] for j in results],
            "top_matches": [
                {"id": j["id"], "title": j["title"], "company": j["company"],
                 "location": j["location"], "match": j.get("match", 0)}
                for j in sorted(results, key=lambda x: x.get("match", 0), reverse=True)[:10]
            ]
        }

    def tool_evaluate_jobs(self, job_ids: list) -> dict:
        """Claude scores jobs — already handled in search, return current scores."""
        evaluated = []
        for jid in job_ids:
            job = self.state["jobs"].get(jid)
            if job:
                # Skip already-applied jobs
                if jid in self.state["applications"]:
                    continue
                evaluated.append({
                    "job_id": jid,
                    "title": job["title"],
                    "company": job["company"],
                    "match": job.get("match", 0),
                    "match_reason": job.get("match_reason", ""),
                    "recommendation": "auto_apply" if job.get("match", 0) >= 85
                                     else "request_approval" if job.get("match", 0) >= 60
                                     else "skip"
                })
        return {"evaluated": len(evaluated), "jobs": evaluated}

    def tool_prepare_application(self, job_id: str) -> dict:
        job = self.state["jobs"].get(job_id)
        if not job:
            return {"error": f"Job {job_id} not found"}
        profile = self.state.get("profile", {})
        log.info(f"Agent: preparing application for {job['title']} at {job['company']}")
        # Generate resume + cover letter
        resume = self.generate_fn("resume", job, profile)
        cover = self.generate_fn("cover", job, profile)
        # Store in state
        if job_id not in self.state["applications"]:
            self.state["applications"][job_id] = {}
        self.state["applications"][job_id].update({
            "tailored_resume": resume,
            "cover_letter": cover,
            "prepared_at": datetime.now().isoformat()
        })
        return {
            "status": "ready",
            "job_id": job_id,
            "title": job["title"],
            "company": job["company"],
            "resume_preview": resume[:200] + "...",
            "cover_preview": cover[:200] + "..."
        }

    def tool_mark_applied(self, job_id: str, method: str = "auto") -> dict:
        job = self.state["jobs"].get(job_id, {})
        self.state["applications"][job_id] = {
            **self.state["applications"].get(job_id, {}),
            "status": "applied",
            "method": method,
            "date": datetime.now().isoformat(),
            "title": job.get("title", ""),
            "company": job.get("company", ""),
        }
        self.state["actions_taken"].append({
            "action": "applied",
            "job_id": job_id,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "method": method,
            "time": datetime.now().isoformat()
        })
        log.info(f"Agent: marked {job.get('title')} at {job.get('company')} as applied ({method})")
        return {"status": "success", "job_id": job_id, "applied_at": datetime.now().isoformat()}

    def tool_draft_followup(self, job_id: str, days_since_applied: int) -> dict:
        job = self.state["jobs"].get(job_id, {})
        profile = self.state.get("profile", {})
        app = self.state["applications"].get(job_id, {})
        log.info(f"Agent: drafting follow-up for {job.get('title')} ({days_since_applied}d ago)")

        try:
            import requests as req
            result = req.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json",
                         "x-api-key": os.environ.get("ANTHROPIC_KEY",""),
                         "anthropic-version": "2023-06-01"},
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 500,
                    "system": "You are an expert career coach writing follow-up emails.",
                    "messages": [{"role": "user", "content":
                        f"Write a brief, professional follow-up email for a job application.\n"
                        f"Job: {job.get('title')} at {job.get('company')}\n"
                        f"Candidate: {profile.get('name')}, {profile.get('title')}\n"
                        f"Applied: {days_since_applied} days ago, no response yet.\n"
                        f"Keep it short (3 sentences), polite, and express continued interest."}]
                },
                timeout=30
            )
            followup_text = result.json()["content"][0]["text"]
        except Exception as e:
            followup_text = f"Dear Hiring Team,\n\nI wanted to follow up on my application for the {job.get('title')} position submitted {days_since_applied} days ago. I remain very interested in this opportunity and would welcome the chance to discuss how my background aligns with your needs.\n\nThank you for your consideration.\n\nBest regards,\n{profile.get('name','')}"

        followup = {
            "job_id": job_id,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "email_text": followup_text,
            "drafted_at": datetime.now().isoformat(),
            "days_since_applied": days_since_applied
        }
        self.state["followups_drafted"].append(followup)
        # Mark as followed up
        if job_id in self.state["applications"]:
            self.state["applications"][job_id]["followed_up"] = True
            self.state["applications"][job_id]["followup_drafted_at"] = datetime.now().isoformat()

        return {"status": "drafted", "job_id": job_id, "preview": followup_text[:200]}

    def tool_request_approval(self, job_id: str, reason: str, recommendation: str) -> dict:
        job = self.state["jobs"].get(job_id, {})
        approval_request = {
            "job_id": job_id,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "match": job.get("match", 0),
            "match_reason": job.get("match_reason", ""),
            "reason": reason,
            "recommendation": recommendation,
            "requested_at": datetime.now().isoformat()
        }
        self.state["pending_approval"].append(approval_request)
        if job_id not in self.state["applications"]:
            self.state["applications"][job_id] = {}
        self.state["applications"][job_id]["status"] = "pending_approval"
        log.info(f"Agent: requesting approval for {job.get('title')} at {job.get('company')}")
        return {"status": "pending", "job_id": job_id}

    # ── MAIN AGENT LOOP ───────────────────────────────
    def run(self, profile: dict, saved_state: dict = None) -> dict:
        """
        Main agent loop. Claude reasons and calls tools until done.
        Returns final state with all actions taken.
        """
        import requests as req

        self.state["profile"] = profile
        if saved_state:
            self.load_state(saved_state)

        # Build keywords from profile
        title = (profile.get("title") or "").lower()
        skills = " ".join(profile.get("skills", [])[:3])
        keywords = f"{title} {skills}".strip() or "senior engineer europe"

        state_summary = self.get_state_summary()

        system_prompt = f"""You are an autonomous job search agent. Your ONLY goal:
Get interview callbacks for {profile.get('name', 'the candidate')}.

CANDIDATE: {profile.get('title')}, {profile.get('experience_years')} years experience
SKILLS: {', '.join(profile.get('skills', [])[:8])}

AVAILABLE TOOLS (use ONLY these — do NOT mention or reference LinkedIn, Indeed, Glassdoor, or any other boards):
1. search_jobs — searches Arbeitnow, Remotive, WeWorkRemotely, The Muse, Adzuna
2. evaluate_jobs — scores jobs against candidate profile
3. prepare_application — rewrites resume + cover letter for a specific job
4. mark_applied — records an application
5. draft_followup — writes follow-up email for old applications
6. request_human_approval — asks user to review medium-match jobs
7. finish — ends the run with a summary

CURRENT STATE:
{state_summary}

MANDATORY SEQUENCE — follow this EXACTLY:
Step 1: Call search_jobs with keywords='{keywords}' and sources=['arbeitnow','remotive','weworkremotely','themuse','adzuna']
Step 2: Call evaluate_jobs with ALL job_ids returned from search
Step 3: For each job with score ≥85: call prepare_application then mark_applied(method='auto')
Step 4: For each job with score 60-84: call request_human_approval
Step 5: For applications older than 7 days: call draft_followup
Step 6: Call finish with summary of what was done

CRITICAL RULES:
- You MUST call search_jobs FIRST — do not skip it
- Do NOT invent job boards — only use the search_jobs tool
- Do NOT say "no jobs found" without actually calling search_jobs first
- Always prepare_application BEFORE mark_applied
- Be concise in tool calls"""

        messages = [{"role": "user", "content":
            f"Start now. Call search_jobs immediately with keywords='{keywords}'. "
            f"Do NOT skip any steps. Do NOT reference job boards other than through the search_jobs tool."}]

        # Agent loop — Claude keeps calling tools until it calls finish
        max_iterations = 20
        iteration = 0
        final_summary = ""

        while iteration < max_iterations:
            iteration += 1
            log.info(f"Agent iteration {iteration}")

            response = req.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json",
                         "x-api-key": os.environ.get("ANTHROPIC_KEY",""),
                         "anthropic-version": "2023-06-01"},
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 1500,
                    "system": system_prompt,
                    "tools": TOOLS,
                    "messages": messages
                },
                timeout=60
            )

            if response.status_code != 200:
                log.error(f"Claude API error: {response.status_code}")
                break

            resp_data = response.json()
            resp_content = resp_data.get("content", [])

            # Add assistant response to messages
            messages.append({"role": "assistant", "content": resp_content})

            # Check stop reason
            stop_reason = resp_data.get("stop_reason")

            # Process tool calls
            tool_results = []
            done = False

            for block in resp_content:
                if block.get("type") == "tool_use":
                    tool_name = block.get("name")
                    tool_input = block.get("input", {})
                    tool_use_id = block.get("id")
                    log.info(f"Agent calling tool: {tool_name}({json.dumps(tool_input)[:100]})")

                    # Execute tool
                    if tool_name == "search_jobs":
                        result = self.tool_search_jobs(
                            tool_input.get("keywords", keywords),
                            tool_input.get("sources")
                        )
                    elif tool_name == "evaluate_jobs":
                        result = self.tool_evaluate_jobs(tool_input.get("job_ids", []))
                    elif tool_name == "prepare_application":
                        result = self.tool_prepare_application(tool_input.get("job_id"))
                    elif tool_name == "mark_applied":
                        result = self.tool_mark_applied(
                            tool_input.get("job_id"),
                            tool_input.get("method", "auto")
                        )
                    elif tool_name == "draft_followup":
                        result = self.tool_draft_followup(
                            tool_input.get("job_id"),
                            tool_input.get("days_since_applied", 7)
                        )
                    elif tool_name == "request_human_approval":
                        result = self.tool_request_approval(
                            tool_input.get("job_id"),
                            tool_input.get("reason", ""),
                            tool_input.get("recommendation", "apply")
                        )
                    elif tool_name == "finish":
                        final_summary = tool_input.get("summary", "Agent run complete")
                        done = True
                        result = {"status": "done"}
                    else:
                        result = {"error": f"Unknown tool: {tool_name}"}

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(result)
                    })

            if done:
                break

            # If no tool calls and stop_reason is end_turn, we're done
            if stop_reason == "end_turn" and not tool_results:
                break

            # Add tool results to messages for next iteration
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        # Build final output
        applied = [a for a in self.state["actions_taken"] if a.get("action") == "applied"]
        return {
