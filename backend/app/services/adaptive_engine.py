import json
import re
from difflib import SequenceMatcher
from typing import Dict, Any, List
from groq import Groq
from backend.app.core.config import settings
from backend.app.schemas.question import AdaptiveEngineDecision, QuestionItem

ADAPTIVE_SYSTEM_PROMPT = """You are an adaptive technical interview engine for an AI interviewer platform.

Your task is to evaluate the candidate's answer to the previous question and decide the NEXT question.

CONTEXT RULE
- The user message contains "session_json", which is the complete interview session JSON.
- Read and use every question, submission, transcript, evaluation, and topic/state entry in "session_json".
- Do not base the decision only on "transcript", "current_question", or the most recent answer.
- Process "topic_history" sequentially: finish the current topic before moving to the next topic.
- Never add another question to a topic whose status is "complete" or whose question_count is 3.
- Use earlier answers to identify repeated gaps, strengths, topic coverage, and follow-up progress.

INTERVIEW STRUCTURE (STRICT ORDER)
- The interview has exactly 6 topics, asked in this fixed order:
  - Topics 1-2: JOB_ROLE (core concepts, responsibilities, tools, and practices of the candidate's SELECTED JOB ROLE)
  - Topics 3-4: TECHNICAL_SKILLS (technologies, languages, frameworks, and tools from the candidate's resume skills)
  - Topics 5-6: projects (projects and experience from the candidate's resume)
- Never change this order. Finish all topics of one type before moving to the next type.
- Each topic gets a MAXIMUM of 3 questions. Never ask a 4th question on any topic.
- Never introduce more than 6 topics.
- Choose each topic from the supplied topic plan for its type. If the plan has no topic for a type, pick one yourself from the selected job role or the resume.
- Never reuse a topic that has already been covered.

FOLLOW-UP DISTINCTNESS (STRICT)
- Never repeat the current question, even with only minor wording changes.
- Before generating a follow-up, compare it against every previous question under the same topic.
- If an answer is incomplete or incorrect, ask for the missing concept in a new way, using a concrete scenario, trade-off, example, or a related deeper concept.
- A valid follow-up must test a different angle from the previous question and must not merely restate, paraphrase, or reverse-engineer it.
- If the previous question and its follow-up need a new context, use a practical situation tied to the selected role, resume, or project instead of repeating the original wording.
- Do not ask another question that is substantively the same as any question already present in the topic history.
- Never copy evaluation text, phrases such as "Did not discuss", or knowledge-gap labels into a question.
- Make questions natural and candidate-facing. Ask a concept, scenario, design choice, or project detail.
- The final two topics PROJECT must ask about the candidate's resume projects, architecture, implementation decisions, trade-offs, or validation.
- TECHNICAL_SKILLS questions must ask about the named skill or concept, never about the generic gap label.

QUESTION STYLE (STRICT)
- This is a THEORY-ONLY interview. Every question must be answerable verbally by explanation.
- NEVER ask the candidate to write, code, implement, debug, complete, or fix code.
- NEVER ask for pseudocode, SQL queries, commands, syntax, formulas to derive, or algorithm implementations.
- NEVER include code snippets for the candidate to read or analyze.
- Instead, ask conceptual questions such as: "what is", "why", "how does X work", "explain the difference between X and Y", "when would you choose X over Y", "what are the trade-offs", "how would you design/approach/architect...", "what happens internally when...".
- For skills like Python, Java, SQL, etc., test concepts (e.g. memory management, indexing, normalization, OOP principles), not the ability to write code.
- For follow-ups, probe deeper into the concept, reasoning, or design trade-offs, never ask them to write code.

TOPIC PROGRESSION (decide in this order)
1. Topic counts and the topic plan are supplied in the input. Follow them exactly.
2. If NLP Score >= 80 (answer is ~80-90%+ accurate and complete):
   - The topic is SATISFIED. Move to the NEXT topic.
3. If NLP Score < 80 (answer is unclear, incomplete, or partly wrong):
   - Ask a FOLLOW-UP in the SAME topic, targeting the exact gap in the answer.
   - Exception: if this topic already has 3 questions, move to the NEXT topic regardless of score.
4. If the current topic is topic 6 and it is satisfied or has 3 questions, set "interview_complete" to true.
5. When moving to the NEXT topic, follow the fixed order: topics 1-2 JOB_ROLE, topics 3-4 TECHNICAL_SKILLS, topics 5-6 PROJECT. Set "topic_type" to match the topic number.

DIFFICULTY RULES
1. Difficulty depends ONLY on technical correctness and relevance (NLP Score) and answer content.
2. STRONG answer (NLP Score >= 80): raise difficulty (EASY -> MEDIUM -> HARD). Next question should go deeper or cover advanced/architectural concepts.
3. WEAK answer (NLP Score < 65): lower difficulty (HARD -> MEDIUM -> EASY). Test fundamentals or the exact knowledge gap.
4. MODERATE answer (65 <= NLP Score < 80): keep the same difficulty.
5. Never adjust difficulty based on speaking speed, head movement, or other non-content signals.
6. Difficulty changes conceptual depth only (basic definitions -> internals -> architecture/trade-offs), never by adding coding tasks.

CONTEXTUAL PROBING
- If the candidate explicitly mentions a technology, framework, or tool (e.g. "I used Docker and Kubernetes"),
  the follow-up should probe that specific technology conceptually (why they chose it, how it works, trade-offs).
- A new topic's first question should start at the current difficulty level, not jump ahead.

OUTPUT FORMAT (strict JSON, no extra text)
{
  "decision": "FOLLOW_UP" | "NEXT_TOPIC" | "END",
  "topic": "<topic for the next question>",
  "topic_type": "JOB_ROLE" | "TECHNICAL_SKILLS" | "PROJECT",
  "difficulty": "EASY" | "MEDIUM" | "HARD",
  "question": "<the next question, theory/conceptual only, no coding task>",
  "reason": "<one sentence justifying the decision>",
  "interview_complete": true | false
}
"""

class AdaptiveEngine:
    def __init__(self):
        self.api_key = settings.GROQ_API_KEY
        self.client = Groq(api_key=self.api_key) if self.api_key and not self.api_key.startswith("gsk_demo") else None

    def decide_next_question(
        self,
        session_id: str,
        role: str,
        resume_context: Dict[str, Any],
        current_question: Dict[str, Any],
        transcript: str,
        nlp_score: int,
        speech_score: int,
        video_score: int,
        technical_strengths: List[str],
        knowledge_gaps: List[str],
        mentioned_topics: List[str],
        difficulty_level: str,
        question_number: int,
        topic_plan: List[str],
        topic_counts: Dict[str, int],
        current_topic_index: int,
        needs_followup: bool = True,
        session_json: Dict[str, Any] = None
    ) -> AdaptiveEngineDecision:
        """Evaluate previous performance and adaptively generate the next question."""
        
        # Rule-based difficulty progression logic
        if nlp_score >= 80:
            if difficulty_level == "EASY":
                target_difficulty = "MEDIUM"
            elif difficulty_level == "MEDIUM":
                target_difficulty = "HARD"
            else:
                target_difficulty = "HARD"
            default_strategy = "FOLLOW_UP"
        elif nlp_score < 65:
            if difficulty_level == "HARD":
                target_difficulty = "MEDIUM"
            else:
                target_difficulty = "EASY"
            default_strategy = "REMEDIAL"
        else:
            target_difficulty = difficulty_level
            default_strategy = "NEW_TOPIC"

        if not self.client:
            # Fallback static adaptive generation if Groq client not active
            if knowledge_gaps and nlp_score < 65:
                gap = knowledge_gaps[0]
                reason = f"Candidate demonstrated a knowledge gap in {gap}."
                current_topic = topic_plan[current_topic_index] if current_topic_index < len(topic_plan) else "Technical Fundamentals"
                clean_gap = self._clean_gap_for_question(gap, current_topic)
                next_q = f"Using a real {role} scenario, explain how you would apply {clean_gap} and identify its practical trade-offs."
                strategy = "REMEDIAL"
                topic = current_topic
            elif mentioned_topics:
                top = mentioned_topics[0]
                reason = f"Candidate mentioned experience with {top}."
                next_q = f"You mentioned working with {top}. Can you explain how you configured and optimized it in your project?"
                strategy = "FOLLOW_UP"
                topic = top
            else:
                reason = f"Candidate showed {target_difficulty.lower()} mastery level in previous technical question."
                next_q = f"In system design for a {role}, how do you handle asynchronous task processing and message queues?"
                strategy = default_strategy
                topic = "System Architecture"

            previous_questions = self._get_previous_questions(session_json or {})
            previous_questions.append(current_question.get("text", ""))
            next_q = self._ensure_unique_fallback_question(
                next_q, role, topic, question_number, knowledge_gaps, previous_questions
            )

            return AdaptiveEngineDecision(
                next_difficulty=target_difficulty,
                question_strategy=strategy,
                target_topic=topic,
                reason=reason,
                next_question=next_q
            )

        previous_questions = self._get_previous_questions(session_json or {})
        current_question_text = current_question.get("text", "")
        if current_question_text and current_question_text not in previous_questions:
            previous_questions.append(current_question_text)

        payload = {
            "session_id": session_id,
            "role": role,
            "resume_context": resume_context,
            "current_question": current_question,
            "transcript": transcript,
            "nlp_score": nlp_score,
            "speech_score": speech_score,
            "video_score": video_score,
            "technical_strengths": technical_strengths,
            "knowledge_gaps": knowledge_gaps,
            "mentioned_topics": mentioned_topics,
            "current_difficulty": difficulty_level,
            "recommended_next_difficulty": target_difficulty,
            "question_number": question_number,
            "topic_plan": topic_plan,
            "topic_counts": topic_counts,
            "current_topic_index": current_topic_index,
            "current_topic": topic_plan[current_topic_index] if current_topic_index < len(topic_plan) else None,
            "needs_followup": needs_followup,
            "session_json": session_json or {},
            "topic_history": self._build_topic_history(session_json or {}, topic_plan, topic_counts),
            "previous_questions": previous_questions,
            "forbidden_questions": previous_questions,
            "max_topics": 6,
            "max_questions_per_topic": 3
        }

        user_prompt = f"Adaptive Interview State:\n{json.dumps(payload, indent=2)}"

        try:
            parsed = None
            generated_question = ""
            for attempt in range(2):
                retry_text = ""
                if attempt:
                    retry_text = (
                        "\nSTRICT RETRY: The previous generated question matched a forbidden question. "
                        "Generate a substantively different question now. Do not repeat or paraphrase any "
                        "question in forbidden_questions."
                    )
                response = self.client.chat.completions.create(
                    model=settings.GROQ_LLM_MODEL,
                    max_tokens=1500,
                    temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": ADAPTIVE_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt + retry_text}
                    ]
                )
                raw = response.choices[0].message.content.strip()
                parsed = json.loads(raw)
                generated_question = (
                    parsed.get("question")
                    or parsed.get("next_question")
                    or f"Can you elaborate further on {role} technical concepts?"
                )
                if not self._is_duplicate_question(generated_question, previous_questions):
                    break

            if self._is_duplicate_question(generated_question, previous_questions):
                current_topic = topic_plan[current_topic_index] if current_topic_index < len(topic_plan) else "the current topic"
                gap_focus = knowledge_gaps[0] if knowledge_gaps else "the most important practical concept"
                gap_focus = self._clean_gap_for_question(gap_focus, current_topic)
                gap_focus = "that technical concept" if gap_focus.lower() == current_topic.lower() else gap_focus
                generated_question = (
                    f"In a real {role} scenario, how would your understanding of {gap_focus} for {current_topic} "
                    "change the approach or decision you would take?"
                )
                generated_question = self._ensure_unique_fallback_question(
                    generated_question,
                    role,
                    current_topic,
                    question_number,
                    knowledge_gaps,
                    previous_questions,
                )

            return AdaptiveEngineDecision(
                next_difficulty=(parsed.get("difficulty") or parsed.get("next_difficulty") or target_difficulty).upper(),
                question_strategy=parsed.get("decision") or parsed.get("question_strategy") or default_strategy,
                target_topic=parsed.get("topic") or parsed.get("target_topic") or "Technical",
                reason=parsed.get("reason", "Adaptive LLM decision based on candidate performance."),
                next_question=generated_question
            )
        except Exception as e:
            print(f"Adaptive engine Groq error: {e}")
            return AdaptiveEngineDecision(
                next_difficulty=target_difficulty,
                question_strategy=default_strategy,
                target_topic="Software Engineering",
                reason=f"Fallback adaptive decision due to API timeout: {e}",
                next_question=self._ensure_unique_fallback_question(
                    f"How would you handle a real {role} technical problem?",
                    role,
                    topic_plan[current_topic_index] if current_topic_index < len(topic_plan) else "Software Engineering",
                    question_number,
                    knowledge_gaps,
                    previous_questions,
                )
            )

    @staticmethod
    def _normalize_question(question: str) -> str:
        normalized = re.sub(r"[^\w\s]", " ", (question or "").lower())
        return " ".join(normalized.split())

    @classmethod
    def _is_duplicate_question(cls, question: str, previous_questions: List[str]) -> bool:
        candidate = cls._normalize_question(question)
        if not candidate:
            return True
        for previous in previous_questions:
            existing = cls._normalize_question(previous)
            if not existing:
                continue
            if candidate == existing:
                return True
            similarity = SequenceMatcher(None, candidate, existing).ratio()
            if similarity >= 0.82 or candidate in existing or existing in candidate:
                return True
        return False

    @staticmethod
    def _get_previous_questions(session_json: Dict[str, Any]) -> List[str]:
        return [
            question.get("question")
            for question in (session_json.get("questions") or [])
            if question.get("question")
        ]

    @classmethod
    def _ensure_unique_fallback_question(
        cls,
        proposed_question: str,
        role: str,
        topic: str,
        question_number: int,
        knowledge_gaps: List[str],
        previous_questions: List[str],
    ) -> str:
        angles = [
            f"Using a real-world {role} scenario, explain the most important trade-offs involved in {topic}.",
            f"When would a different approach to {topic} be preferable, and how would you justify that decision?",
            f"How would you validate that your understanding of {topic} is correct before applying it in a project?",
        ]

        candidates = [proposed_question, *angles]
        for candidate in candidates:
            if not cls._is_duplicate_question(candidate, previous_questions):
                return candidate
        return angles[(question_number or 0) % len(angles)]

    @staticmethod
    def _clean_gap_for_question(gap: str, topic: str) -> str:
        clean = " ".join((gap or "").strip().split())
        prefixes = (
            "did not discuss", "did not address", "did not cover", "missed",
            "lacked", "no mention of", "candidate did not", "not demonstrated",
        )
        lower = clean.lower()
        for prefix in prefixes:
            if lower.startswith(prefix):
                clean = clean[len(prefix):].strip(" :-")
                lower = clean.lower()
                break
        for prefix in ("the practical impact of ", "practical impact of ", "the understanding of ", "knowledge of "):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):].strip(" :-")
                break
        clean = clean.split(" in ", 1)[0].split(" for ", 1)[0].strip()
        topic_normalized = topic.strip().lower()
        clean_normalized = clean.strip().lower()
        if clean_normalized == topic_normalized or clean_normalized.startswith(f"{topic_normalized} on {topic_normalized}"):
            clean = topic
        return clean if clean and len(clean) <= 80 else topic

    @staticmethod
    def _build_topic_history(
        session_json: Dict[str, Any],
        topic_plan: List[str],
        topic_counts: Dict[str, int]
    ) -> List[Dict[str, Any]]:
        """Group every question and submission under its topic in interview order."""
        questions = session_json.get("questions") or []
        submissions = session_json.get("submissions") or {}
        grouped: Dict[str, List[Dict[str, Any]]] = {}

        for question in questions:
            topic = question.get("topic") or "General"
            question_id = question.get("id")
            submission = submissions.get(str(question_id), submissions.get(question_id))

            grouped.setdefault(topic, []).append({
                "question_id": question_id,
                "question": question.get("question"),
                "difficulty": question.get("difficulty"),
                "submission": submission or None,
            })

        history = []
        for topic in topic_plan:
            entries = grouped.get(topic, [])
            count = len(entries)
            if count >= 3 or topic_counts.get(topic, 0) >= 3:
                status = "complete"
            elif count > 0:
                status = "in_progress"
            else:
                status = "not_started"

            history.append({
                "topic": topic,
                "status": status,
                "question_count": count,
                "questions": entries,
            })
        return history

adaptive_engine = AdaptiveEngine()
