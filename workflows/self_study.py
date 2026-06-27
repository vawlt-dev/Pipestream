# =============================================================================
# Workflow: Self Study
# =============================================================================
# Proactively deepens and broadens the system's generic (persona-level)
# knowledge base, independent of any specific company or outreach task. Two
# passes:
#   1. Gap-filling — for every persona already known, fill in any missing or
#      expired Q1-15/PITCH1-3 rows. A live lead-gen run only ever researches
#      whichever 3 questions select_seed_questions picked for that one run;
#      this pass rounds a persona out more completely over repeated calls.
#   2. Concept-discovery — for each known persona, ask what closely related
#      concepts are worth understanding, then research genuinely new ones at
#      topic_level="persona" too. gather_info()'s own docstring already says
#      its topic can be "anything: a company, person, technology, concept" —
#      this is the first thing that actually exercises that for non-company
#      research, rather than only ever being called with a company name.
#
# Bounded by MAX_ITEMS_DEFAULT per invocation — a deliberate, boundable chunk
# of work, not an unbounded background loop. agent_worker.py is still
# single-task-sequential, so this occupies the worker like any other task for
# as long as it runs; re-trigger this workflow again later to keep going.
# =============================================================================

from core import llm_structured, check_cancelled, gather_info, run_concurrent
from schemas import s_object, s_string, s_array
from research import SEED_QUESTIONS, PITCH_QUESTIONS, answer_question
from memory import memory_list_personas, memory_get_persona_coverage

WORKFLOW_META = {
    "name": "self_study",
    "description": (
        "Proactively deepens and broadens generic (persona-level) knowledge, "
        "independent of any specific company or outreach task. Use when asked "
        "to study, learn, expand its knowledge, fill research gaps, or get "
        "smarter about a domain — with no specific target company/person."
    ),
}

_SEED_TOPIC_SCHEMA        = s_object({"seed_topic": s_string()})
_RELATED_CONCEPTS_SCHEMA  = s_object({"concepts": s_array(s_string())})

# qid -> {X}-templated question text, merged from both question sets —
# memory_get_persona_coverage() doesn't care which set an id came from, it's
# just whatever's actually in the table for a given persona.
_ALL_QUESTION_IDS = {**SEED_QUESTIONS, **PITCH_QUESTIONS}

MAX_ITEMS_DEFAULT = 5


def run(task_id: str, input_text: str, client) -> None:

    def log(msg: str, log_type: str = "info"):
        print(f"  [{log_type.upper()}] {msg}")
        client.log(task_id, msg, log_type)

    log("📚 Starting self-study pass...", "self_study")

    # =========================================================================
    # STEP 1: Optional seed topic from input
    # =========================================================================
    seed_topic = None
    if input_text and input_text.strip():
        parsed = llm_structured(
            f'Does this request name a specific domain/topic to study?\n\n'
            f'Request: "{input_text}"\n\n'
            f'seed_topic: the domain/topic if one is named, or empty string if not',
            _SEED_TOPIC_SCHEMA, schema_name="self_study_seed_topic",
        )
        seed_topic = str(parsed.get("seed_topic") or "").strip() or None

    known = memory_list_personas()

    if seed_topic and seed_topic.lower() not in {k.lower() for k in known}:
        log(f"🌱 Seeding new persona from request: {seed_topic}", "self_study")
        gather_info(
            seed_topic, "persona", task_id, client, log,
            context="Self-directed knowledge expansion, explicitly requested",
        )
        known = memory_list_personas()

    targets = [seed_topic] if seed_topic else known
    if not targets:
        log("Nothing known yet and no seed topic given — nothing to study", "info")
        client.update_status(task_id, "completed", result="No personas known yet, and no seed topic given.")
        return

    items_processed  = 0
    gaps_filled      = 0
    concepts_learned: list[str] = []

    # =========================================================================
    # STEP 2: Gap-filling pass
    # =========================================================================
    for persona in targets:
        if items_processed >= MAX_ITEMS_DEFAULT:
            break
        if check_cancelled(task_id, client):
            return

        coverage = memory_get_persona_coverage(persona)
        fresh    = set(coverage.get("fresh") or [])
        missing  = [qid for qid in _ALL_QUESTION_IDS if qid not in fresh]

        if not missing:
            log(f"'{persona}' already fully covered", "info")
            continue

        log(f"🔍 Filling {len(missing)} gap(s) for '{persona}': {', '.join(missing)}", "self_study")
        gap_tasks = [
            (lambda qid=qid: answer_question(
                persona, "persona", qid, _ALL_QUESTION_IDS[qid].replace("{X}", persona),
                "Self-directed knowledge expansion", task_id, client, log,
            ))
            for qid in missing
        ]
        for row in run_concurrent(gap_tasks):
            if row and row.get("was_answered"):
                gaps_filled += 1
        items_processed += 1

    # =========================================================================
    # STEP 3: Concept-discovery pass
    # =========================================================================
    known       = memory_list_personas()  # refreshed — gap-filling may have added rows
    known_lower = {k.lower() for k in known}

    for persona in targets:
        if items_processed >= MAX_ITEMS_DEFAULT:
            break
        if check_cancelled(task_id, client):
            return

        result = llm_structured(
            f'We already understand "{persona}" reasonably well. What are 3-5 closely '
            f'related concepts, terms, or sub-topics that would be worth understanding '
            f'more deeply to better serve or reason about this domain?',
            _RELATED_CONCEPTS_SCHEMA, schema_name="related_concepts",
        )
        candidates = [str(c).strip() for c in (result.get("concepts") or []) if str(c).strip()]
        log(f"💡 Related to '{persona}': {', '.join(candidates) or '(none)'}", "self_study")

        new_candidates = [c for c in candidates if c.lower() not in known_lower]
        budget         = MAX_ITEMS_DEFAULT - items_processed
        to_research    = new_candidates[:budget]
        if not to_research:
            continue
        if check_cancelled(task_id, client):
            return

        log(f"📖 Researching new concept(s): {', '.join(to_research)}", "self_study")
        concept_tasks = [
            (lambda concept=concept: gather_info(
                concept, "persona", task_id, client, log,
                context=f"Adjacent concept to '{persona}', self-directed knowledge expansion",
            ))
            for concept in to_research
        ]
        run_concurrent(concept_tasks)
        for concept in to_research:
            known_lower.add(concept.lower())
            concepts_learned.append(concept)
        items_processed += len(to_research)

    # =========================================================================
    # Summary
    # =========================================================================
    summary = f"Filled {gaps_filled} gap(s) across {len(targets)} known persona(s)."
    if concepts_learned:
        summary += f" Learned {len(concepts_learned)} new concept(s): {', '.join(concepts_learned)}."

    log(f"✅ {summary}", "success")
    client.update_status(task_id, "completed", result=summary)
