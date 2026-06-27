# =============================================================================
# Workflow: Lead Gen Outreach
# =============================================================================
# Input shape: "Hi, I'm <name>, <title> at <company>. Today our goal is to
# <profiteering goal>. Find <N> companies we could serve and draft outreach
# emails to them."
#
# Researches the sender's own company, discovers prospective companies via
# web search, researches each one, drafts a personalized cold outreach email,
# and saves it as a Gmail draft (never sent automatically — for review).
# =============================================================================

import re

from core import (
    llm_structured, gather_info, format_answers_as_context,
    clean_email_draft, clean_subject_line, extract_greeting_name,
    check_draft_appropriateness, check_cancelled, run_concurrent,
)
from schemas import s_object, s_string, s_int, s_array
from research import find_contact_email
from tools_web import web_search
from tools_google import create_email_draft
from memory import memory_get_contacted_prospects, memory_record_outreach, _normalize

WORKFLOW_META = {
    "name": "lead_gen_outreach",
    "description": (
        "Find multiple prospective client companies for a business and draft a "
        "personalized cold outreach email to each one, saved as Gmail drafts — "
        "never sent automatically. Use when the request introduces a person and "
        "their company, states a business goal, and asks to find prospects, find "
        "companies to reach out to, build a lead list, or draft outreach to "
        "several companies at once. Different from business_intro, which targets "
        "a single company and sends after approval — this targets many companies "
        "and only ever creates drafts."
    ),
}

_COMPANY_LIST_SCHEMA = s_object({"companies": s_array(s_string())})

# Extraction forces a `reason` per candidate — the actual value isn't JSON
# parsing reliability (already solved), it's that requiring a justification
# catches semantically wrong inclusions (e.g. a competitor or an unrelated
# entity that just showed up prominently in the search results).
_CANDIDATE_SCHEMA = s_object({
    "companies": s_array(s_object({
        "name":   s_string(),
        "reason": s_string(),
    })),
})

_PARSED_REQUEST_SCHEMA = s_object({
    "sender_name":    s_string(),
    "sender_title":   s_string(),
    "sender_company": s_string(),
    "goal":           s_string(),
    "count":          s_int(),
})

_SEARCH_QUERIES_SCHEMA = s_object({"queries": s_array(s_string())})
_ANGLE_SCHEMA          = s_object({"angle": s_string()})
_DRAFT_BODY_SCHEMA     = s_object({"body": s_string()})
_SUBJECT_LINE_SCHEMA   = s_object({"subject": s_string()})


# =============================================================================
# MAIN WORKFLOW
# =============================================================================

def run(task_id: str, input_text: str, client) -> None:

    def log(msg: str, log_type: str = 'info'):
        print(f'  [{log_type.upper()}] {msg}')
        client.log(task_id, msg, log_type)

    # =========================================================================
    # STEP 1: Parse sender identity, goal, and target count
    # =========================================================================
    log('📝 Parsing request...', 'info')

    parsed = llm_structured(
        f'Extract the following from this request.\n\n'
        f'Request: "{input_text}"\n\n'
        f'sender_name: the person\'s name\n'
        f'sender_title: their job title\n'
        f'sender_company: their company name\n'
        f'goal: what they want to achieve / who they want to reach, in one line\n'
        f'count: number of companies to find — just the number, default 10 if not specified',
        _PARSED_REQUEST_SCHEMA,
        schema_name="parsed_request",
    )
    log(f'Parsed: {parsed}', 'agent')

    sender_name    = str(parsed.get('sender_name') or '').strip()
    sender_title   = str(parsed.get('sender_title') or '').strip()
    sender_company = str(parsed.get('sender_company') or '').strip()
    goal           = str(parsed.get('goal') or '').strip()
    target_count   = parsed.get('count')

    if not isinstance(target_count, int) or target_count <= 0:
        # Schema marks count as required, but that only enforces type, not
        # that the model actually found a number in the input — fall back to
        # scanning the raw input directly before accepting a default of 10.
        count_match = (
            re.search(r'\bfind\s+(\d+)\b', input_text, re.IGNORECASE)
            or re.search(r'\b(\d+)\s+compan', input_text, re.IGNORECASE)
        )
        target_count = int(count_match.group(1)) if count_match else 10

    if not sender_company:
        log("Could not identify the sender's company from the request", 'error')
        client.update_status(task_id, 'failed',
                             error_message="Could not identify your company — please include it in the request.")
        return

    log(f'Sender: {sender_name} ({sender_title}) @ {sender_company}', 'info')
    log(f'Goal: {goal}', 'info')
    log(f'Target: {target_count} companies', 'info')

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 2: Research the sender's own company
    # =========================================================================
    log(f'🔬 Researching {sender_company}...', 'info')

    own_info = gather_info(
        sender_company, 'prospect', task_id, client, log,
        context=f'Understanding {sender_company} well enough to pitch prospects on its behalf. Goal: {goal}',
    )
    if not own_info:
        return  # cancelled mid-research

    own_context = format_answers_as_context(own_info)
    log(f'{sender_company}: {own_context[:200]}', 'info')

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 3: Generate discovery search queries
    # =========================================================================
    log('🔍 Generating prospect search queries...', 'info')

    queries_result = llm_structured(
        f'A company needs to find prospective CLIENTS — companies that NEED this kind '
        f'of help, not companies that PROVIDE similar or competing services.\n\n'
        f'Their company: {sender_company}\n'
        f'What they do: {own_context}\n'
        f'Goal: {goal}\n\n'
        f'Generate 4 distinct web search queries to find real companies that would '
        f'be good prospects for this goal. Phrase each query around companies '
        f'experiencing the problem or need described in the goal — NOT around '
        f'companies that already offer that same service (those are competitors, '
        f'not prospects). Each query should be aimed at surfacing actual company '
        f'names and websites, not generic articles.',
        _SEARCH_QUERIES_SCHEMA,
        schema_name="search_queries",
    )
    log(f'Search queries: {queries_result}', 'agent')

    queries = [str(q).strip() for q in (queries_result.get('queries') or []) if str(q).strip()][:4]

    def _run_discovery_search(q):
        log(f'🔍 {q}', 'tool_call')
        return f'Search: {q}\n{web_search(q, max_results=8)}'

    search_tasks = [(lambda q=q: _run_discovery_search(q)) for q in queries]
    all_results = run_concurrent(search_tasks)

    if check_cancelled(task_id, client):
        return

    combined_results = '\n\n---\n\n'.join(all_results)

    # =========================================================================
    # STEP 4: Extract candidate company names
    # =========================================================================
    log('📋 Extracting candidate companies...', 'info')

    extracted = llm_structured(
        f'From these search results, extract a list of distinct real company names '
        f'that could be prospects for "{sender_company}" (goal: {goal}).\n\n'
        f'Exclude: {sender_company} itself, directory/listing sites, news aggregators, '
        f'generic terms, AND any company that appears to PROVIDE the same or a '
        f'competing service to {sender_company} rather than NEEDING it — those are '
        f'competitors, not prospects, even if they show up prominently in the results.\n\n'
        f'For each company, give its name and a one-line reason it fits as a prospect '
        f'(not a competitor).\n\n'
        f'Search results:\n{combined_results[:10000]}',
        _CANDIDATE_SCHEMA,
        schema_name="candidate_companies",
    )
    log(f'Candidates: {extracted}', 'agent')
    candidates = extracted.get('companies') or []

    seen = set()
    unique_candidates = []
    for c in candidates:
        name   = str(c.get('name') or '').strip()
        reason = str(c.get('reason') or '').strip()
        key    = name.lower()
        if key and key not in seen and key != sender_company.strip().lower():
            seen.add(key)
            unique_candidates.append(name)
            if reason:
                log(f'  {name}: {reason}', 'info')

    if not unique_candidates:
        log('No candidate companies found', 'error')
        client.update_status(task_id, 'failed',
                             error_message='Could not find any prospective companies for this goal.')
        return

    log(f'Found {len(unique_candidates)} unique candidates', 'tool_result')

    # Exclude prospects already contacted for THIS sender in a prior run —
    # before curation, so the buffer isn't wasted on candidates that would
    # have been filtered out anyway. Any status counts (drafted, or tried
    # and failed for any reason) — see memory_get_contacted_prospects().
    already_contacted = memory_get_contacted_prospects(sender_company)
    if already_contacted:
        before = len(unique_candidates)
        # memory_get_contacted_prospects() returns keys normalized via
        # memory._normalize() (suffix/punctuation-stripped, not just
        # lowercased) — must apply the SAME normalization here, or "Acme Ltd"
        # vs "Acme" would never match and the dedup filter would silently do
        # nothing for exactly the cases _normalize() exists to catch.
        unique_candidates = [c for c in unique_candidates if _normalize(c) not in already_contacted]
        skipped_count = before - len(unique_candidates)
        if skipped_count:
            log(f'Excluding {skipped_count} candidate(s) already contacted for {sender_company}', 'info')

    if not unique_candidates:
        log('All candidates already contacted for this sender — nothing new to target', 'error')
        client.update_status(task_id, 'failed',
                             error_message='Found candidates, but all have already been contacted for this sender.')
        return

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 5: Curate down to a working list (with buffer for dropouts)
    # =========================================================================
    buffer_count = min(len(unique_candidates), target_count + 5)

    if len(unique_candidates) > buffer_count:
        extracted = llm_structured(
            f'From this list of candidate companies, select the {buffer_count} best '
            f'prospects for: {goal}, in priority order.\n\n'
            f'Candidates:\n' + '\n'.join(f'- {c}' for c in unique_candidates),
            _COMPANY_LIST_SCHEMA,
            schema_name="curated_companies",
        )
        curated = extracted.get('companies') or unique_candidates[:buffer_count]
    else:
        curated = unique_candidates

    log(f'Working list: {len(curated)} companies', 'info')

    # =========================================================================
    # STEP 6a: Research every candidate concurrently
    # =========================================================================
    # gather_info() + find_contact_email() per candidate are fully independent
    # of every OTHER candidate — research all of them at once instead of one
    # at a time. Drafting + Gmail draft creation (6b below) stays sequential:
    # Google's API client objects aren't documented thread-safe, and that's
    # exactly the kind of real-side-effect surface not worth the risk here —
    # the LLM-heavy research phase is where the real time is spent anyway.
    log(f'🔬 Researching {len(curated)} candidate(s) concurrently...', 'info')

    def _research_candidate(company_name: str):
        info = gather_info(
            company_name, 'prospect', task_id, client, log,
            context=f'Cold outreach prospect for {sender_company}. Goal: {goal}',
            known_context_hint=own_context,
        )
        if not info:
            return (company_name, None, None)  # cancelled mid-research
        target_email = find_contact_email(company_name, task_id, client, log)
        return (company_name, info, target_email)

    research_tasks = [(lambda c=c: _research_candidate(c)) for c in curated]
    research_results = run_concurrent(research_tasks)

    if check_cancelled(task_id, client):
        return

    # =========================================================================
    # STEP 6b: Draft + create Gmail draft, sequentially, until target_count
    # =========================================================================
    drafts_created:        list[str] = []
    skipped_no_contact:     list[str] = []
    skipped_draft_failed:   list[str] = []
    skipped_inappropriate:  list[str] = []

    for company_name, info, target_email in research_results:
        if len(drafts_created) >= target_count:
            break
        if check_cancelled(task_id, client):
            return
        if not info:
            continue  # cancelled mid-research for this one specifically

        if not target_email:
            log(f'No contact email found for {company_name} — skipping', 'info')
            skipped_no_contact.append(company_name)
            memory_record_outreach(sender_company, company_name, 'skipped_no_contact')
            continue

        research_context = format_answers_as_context(info)

        # --- Angle ---
        angle_result = llm_structured(
            f'Based on this research about {company_name}, identify the single most '
            f'compelling, specific angle for a cold outreach email from {sender_name} '
            f'({sender_title} at {sender_company}).\n\n'
            f'Goal of the outreach: {goal}\n\n'
            f'The angle should be rooted in something concrete — a recent development, '
            f'what makes this company distinctive, a challenge {sender_company} could '
            f'help with. Not generic flattery.\n\n'
            f'Research:\n{research_context}\n\n'
            f'angle: a short phrase describing the angle, no quotes',
            _ANGLE_SCHEMA,
            schema_name="outreach_angle",
        )
        angle = str(angle_result.get('angle') or '').strip().strip('"').strip("'")
        log(f'Angle for {company_name}: {angle}', 'agent')

        # Best-effort — only available if Q3 (stakeholders) was one of the 3
        # questions selected for this topic, AND the answer clearly names a
        # person rather than just describing the company. Falls back to a
        # generic greeting otherwise.
        key_person = next(
            (a['answer'] for a in info['answers'] if a['question_id'] == 'Q3' and a['was_answered']),
            'not found',
        )
        greeting_name = extract_greeting_name(key_person)
        greeting = f'Hi {greeting_name}' if greeting_name else 'Hi'

        # --- Draft body ---
        def _generate_draft_body(extra_feedback: str = '') -> str:
            feedback_line = (
                f'\nPREVIOUS ATTEMPT HAD THIS PROBLEM — avoid it this time: {extra_feedback}\n'
                if extra_feedback else ''
            )
            draft_result = llm_structured(
                f'Write a short, professional cold outreach email to {company_name}.\n\n'
                f'The email should be anchored around this angle: {angle}\n\n'
                f'Context:\n'
                f'- Target company: {company_name}\n{research_context}\n'
                f'- Outreach goal: {goal}\n\n'
                f'About the sender:\n'
                f'- Name: {sender_name}\n'
                f'- Title: {sender_title} at {sender_company}\n'
                f'- What {sender_company} does: {own_context}\n\n'
                f'{feedback_line}'
                f'Guidelines:\n'
                f'- 3-5 sentences max\n'
                f'- Open by acknowledging the angle naturally\n'
                f'- Be warm and direct, not corporate\n'
                f'- End with a soft call to action\n'
                f'- Do NOT include a greeting, signature, or subject line\n'
                f'- Do NOT invent or guess at names, emails, phone numbers, or other '
                f'contact details that aren\'t given to you above — omit them rather '
                f'than making them up\n\n'
                f'body: the email body only',
                _DRAFT_BODY_SCHEMA,
                schema_name="email_draft_body",
            )
            return clean_email_draft(str(draft_result.get('body') or ''))

        draft_body = _generate_draft_body()

        # Content-safety gate, separate from the no-fabrication instruction
        # above — that guards against invented CONTACT DETAILS, this guards
        # against presumptuous/offensive CLAIMS (the real example that
        # motivated this: a draft asserting something like "your workers are
        # suffering from mental health issues" as a pitch angle). One retry
        # with the concern fed back as corrective feedback, then skip.
        safety = check_draft_appropriateness(draft_body, company_name)
        if not safety['appropriate']:
            log(f"⚠️ Draft flagged: {safety['concern']} — regenerating once", 'info')
            draft_body = _generate_draft_body(extra_feedback=safety['concern'])
            safety = check_draft_appropriateness(draft_body, company_name)
            if not safety['appropriate']:
                log(f"✗ Still flagged after retry: {safety['concern']} — skipping", 'error')
                skipped_inappropriate.append(company_name)
                memory_record_outreach(sender_company, company_name, 'skipped_inappropriate')
                continue

        subject_result = llm_structured(
            f'Write a short email subject line (under 50 characters) for outreach to '
            f'{company_name} about: {angle}\n'
            f'No emojis, no quotes.',
            _SUBJECT_LINE_SCHEMA,
            schema_name="subject_line",
        )
        subject_line = clean_subject_line(str(subject_result.get('subject') or ''))

        full_email = (
            f'{greeting},\n\n'
            f'{draft_body}\n\n'
            f'Best regards,\n'
            f'{sender_name}\n'
            f'{sender_title}\n'
            f'{sender_company}'
        )

        log(f'📤 Creating Gmail draft for {target_email}...', 'tool_call')
        result = create_email_draft(target_email, subject_line, full_email)
        log(result, 'tool_result' if '✅' in result else 'error')

        if '✅' in result:
            memory_record_outreach(
                sender_company, company_name, 'drafted',
                target_email=target_email, subject_line=subject_line,
                angle=angle, draft_body=full_email,
            )
            drafts_created.append(company_name)
        else:
            skipped_draft_failed.append(company_name)
            memory_record_outreach(sender_company, company_name, 'skipped_draft_failed')

        if check_cancelled(task_id, client):
            return

    # =========================================================================
    # Summary
    # =========================================================================
    summary = f'Created {len(drafts_created)} draft(s)'
    if drafts_created:
        summary += f': {", ".join(drafts_created)}.'
    if skipped_no_contact:
        summary += f' Skipped {len(skipped_no_contact)} (no contact found): {", ".join(skipped_no_contact)}.'
    if skipped_draft_failed:
        summary += f' Skipped {len(skipped_draft_failed)} (found contact but draft creation failed): {", ".join(skipped_draft_failed)}.'
    if skipped_inappropriate:
        summary += f' Skipped {len(skipped_inappropriate)} (flagged content concern): {", ".join(skipped_inappropriate)}.'

    log(f'✅ {summary}', 'success')
    client.update_status(task_id, 'completed', result=summary)
