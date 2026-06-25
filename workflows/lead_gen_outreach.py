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
    llm_call, llm_structured, gather_info, format_answers_as_context,
    clean_email_draft, clean_subject_line, extract_greeting_name,
    check_cancelled,
)
from research import find_contact_email
from tools_web import web_search
from tools_google import create_email_draft

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

_COMPANY_LIST_SCHEMA = {
    "type": "object",
    "properties": {"companies": {"type": "array", "items": {"type": "string"}}},
    "required": ["companies"],
    "additionalProperties": False,
}


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
        {
            "type": "object",
            "properties": {
                "sender_name":    {"type": "string"},
                "sender_title":   {"type": "string"},
                "sender_company": {"type": "string"},
                "goal":           {"type": "string"},
                "count":          {"type": "integer"},
            },
            "required": ["sender_name", "sender_title", "sender_company", "goal", "count"],
            "additionalProperties": False,
        },
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

    queries_raw = llm_call(
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
        f'names and websites, not generic articles.\n\n'
        f'Reply with one query per line, no numbering, no extra text.'
    )
    log(f'Search queries:\n{queries_raw}', 'agent')

    queries = [q.strip().strip('-').strip() for q in queries_raw.splitlines() if q.strip()][:4]

    all_results = []
    for q in queries:
        log(f'🔍 {q}', 'tool_call')
        result = web_search(q, max_results=8)
        all_results.append(f'Search: {q}\n{result}')
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
        f'Search results:\n{combined_results[:10000]}',
        _COMPANY_LIST_SCHEMA,
        schema_name="candidate_companies",
    )
    log(f'Candidates: {extracted}', 'agent')
    candidate_names = extracted.get('companies') or []

    seen = set()
    unique_candidates = []
    for name in candidate_names:
        name = str(name).strip()
        key  = name.lower()
        if key and key not in seen and key != sender_company.strip().lower():
            seen.add(key)
            unique_candidates.append(name)

    if not unique_candidates:
        log('No candidate companies found', 'error')
        client.update_status(task_id, 'failed',
                             error_message='Could not find any prospective companies for this goal.')
        return

    log(f'Found {len(unique_candidates)} unique candidates', 'tool_result')

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
    # STEP 6: Research + draft each candidate until target_count drafts exist
    # =========================================================================
    drafts_created:      list[str] = []
    skipped_no_contact:   list[str] = []
    skipped_draft_failed: list[str] = []

    for company_name in curated:
        if len(drafts_created) >= target_count:
            break
        if check_cancelled(task_id, client):
            return

        log(f'─── Researching: {company_name}', 'info')
        info = gather_info(
            company_name, 'prospect', task_id, client, log,
            context=f'Cold outreach prospect for {sender_company}. Goal: {goal}',
        )
        if not info:
            return  # cancelled mid-research

        target_email = find_contact_email(company_name, task_id, client, log)

        if not target_email:
            log(f'No contact email found for {company_name} — skipping', 'info')
            skipped_no_contact.append(company_name)
            continue

        research_context = format_answers_as_context(info)

        # --- Angle ---
        angle = llm_call(
            f'Based on this research about {company_name}, identify the single most '
            f'compelling, specific angle for a cold outreach email from {sender_name} '
            f'({sender_title} at {sender_company}).\n\n'
            f'Goal of the outreach: {goal}\n\n'
            f'The angle should be rooted in something concrete — a recent development, '
            f'what makes this company distinctive, a challenge {sender_company} could '
            f'help with. Not generic flattery.\n\n'
            f'Research:\n{research_context}\n\n'
            f'Reply with ONLY a short phrase describing the angle. No quotes, no explanation:'
        ).strip().strip('"').strip("'")
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
        draft_body = clean_email_draft(llm_call(
            f'Write a short, professional cold outreach email to {company_name}.\n\n'
            f'The email should be anchored around this angle: {angle}\n\n'
            f'Context:\n'
            f'- Target company: {company_name}\n{research_context}\n'
            f'- Outreach goal: {goal}\n\n'
            f'About the sender:\n'
            f'- Name: {sender_name}\n'
            f'- Title: {sender_title} at {sender_company}\n'
            f'- What {sender_company} does: {own_context}\n\n'
            f'Guidelines:\n'
            f'- 3-5 sentences max\n'
            f'- Open by acknowledging the angle naturally\n'
            f'- Be warm and direct, not corporate\n'
            f'- End with a soft call to action\n'
            f'- Do NOT include a greeting, signature, or subject line\n'
            f'- Do NOT invent or guess at names, emails, phone numbers, or other '
            f'contact details that aren\'t given to you above — omit them rather '
            f'than making them up\n\n'
            f'Write ONLY the email body:'
        ))

        subject_result = llm_structured(
            f'Write a short email subject line (under 50 characters) for outreach to '
            f'{company_name} about: {angle}\n'
            f'No emojis, no quotes.',
            {
                "type": "object",
                "properties": {"subject": {"type": "string"}},
                "required": ["subject"],
                "additionalProperties": False,
            },
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
            drafts_created.append(company_name)
        else:
            skipped_draft_failed.append(company_name)

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

    log(f'✅ {summary}', 'success')
    client.update_status(task_id, 'completed', result=summary)
