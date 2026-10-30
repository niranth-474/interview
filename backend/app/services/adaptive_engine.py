import json
from typing import Dict, Any, List
from groq import Groq
from backend.app.core.config import settings
from backend.app.schemas.question import AdaptiveEngineDecision, QuestionItem

ADAPTIVE_SYSTEM_PROMPT = """You are an adaptive technical interview engine for an AI interviewer platform.

Your task is to evaluate the candidate's answer to the previous question and decide the NEXT question.

INTERVIEW STRUCTURE
- The interview covers exactly 5 topics, chosen from the supplied topic plan.
- EXACTLY 2 of the 5 topics must be based on the candidate's SELECTED JOB ROLE (core concepts, responsibilities, tools, and practices of that role).
- The other 3 topics come from the candidate's resume and skills (projects, technologies, experience).
- Each topic gets a MAXIMUM of 3 questions. Never ask a 4th question on any topic.
- Never introduce a 6th topic.
- Never drop below 2 job-role topics. If the plan supplies fewer than 2, pick job-role topics yourself before moving on.

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
4. If the current topic is the 5th topic and it is satisfied or has 3 questions, set "interview_complete" to true.
5. When choosing the NEXT topic, make sure the 2 job-role topics are still covered before the interview ends.

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
  "topic_type": "JOB_ROLE" | "RESUME_SKILLS",
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
        needs_followup: bool = True
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
                next_q = f"Could you walk me through the core fundamentals of {gap} and how you would apply it in a {role} role?"
                strategy = "REMEDIAL"
                topic = gap
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

            return AdaptiveEngineDecision(
                next_difficulty=target_difficulty,
                question_strategy=strategy,
                target_topic=topic,
                reason=reason,
                next_question=next_q
            )

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
            "max_questions_per_topic": 4
        }

        user_prompt = f"Adaptive Interview State:\n{json.dumps(payload, indent=2)}"

        try:
            response = self.client.chat.completions.create(
                model=settings.GROQ_LLM_MODEL,
                max_tokens=1500,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": ADAPTIVE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ]
            )
            raw = response.choices[0].message.content.strip()
            parsed = json.loads(raw)
            return AdaptiveEngineDecision(
                next_difficulty=parsed.get("next_difficulty", target_difficulty),
                question_strategy=parsed.get("question_strategy", default_strategy),
                target_topic=parsed.get("target_topic", "Technical"),
                reason=parsed.get("reason", "Adaptive LLM decision based on candidate performance."),
                next_question=parsed.get("next_question", f"Can you elaborate further on {role} technical concepts?")
            )
        except Exception as e:
            print(f"Adaptive engine Groq error: {e}")
            return AdaptiveEngineDecision(
                next_difficulty=target_difficulty,
                question_strategy=default_strategy,
                target_topic="Software Engineering",
                reason=f"Fallback adaptive decision due to API timeout: {e}",
                next_question=f"For a {role} position, how do you approach debugging production incidents?"
            )

adaptive_engine = AdaptiveEngine()
