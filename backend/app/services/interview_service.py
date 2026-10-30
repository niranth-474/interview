import logging
import concurrent.futures
from collections import defaultdict
from uuid import uuid4
from pathlib import Path
from typing import Optional, Dict, Any, List
from backend.app.schemas.interview import AssessmentSession, AnswerSubmission
from backend.app.schemas.question import QuestionItem
from backend.app.core.constants import AssessmentType
from backend.app.storage.session_storage import session_storage
from backend.app.storage.file_storage import file_storage
from backend.app.services.resume_service import resume_service
from backend.app.services.question_service import question_service
from backend.app.services.audio_service import audio_service
from backend.app.services.speech_service import speech_service
from backend.app.services.video_service import video_service
from backend.app.services.nlp_service import nlp_service
from backend.app.services.adaptive_engine import adaptive_engine
from backend.app.services.performance_service import performance_service
from fastapi import HTTPException, status

logger = logging.getLogger("interview_service")

_video_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="video_analysis")

MAX_TOPICS = 4
MAX_QUESTIONS_PER_TOPIC = 4
DEFAULT_TOPIC_NAMES = [
    "Core Fundamentals",
    "Problem Solving",
    "System Design",
    "Best Practices",
]


def build_topic_plan(role: str, resume_data: Dict[str, Any], first_topic: str) -> List[str]:
    candidates: List[str] = [first_topic]
    for skill in resume_data.get("skills", []) or []:
        if isinstance(skill, str) and skill.strip():
            candidates.append(skill.strip())

    candidates.extend([f"{role} Fundamentals", *DEFAULT_TOPIC_NAMES])

    plan: List[str] = []
    seen = set()
    for candidate in candidates:
        normalized = candidate.strip()
        key = normalized.lower()
        if normalized and key not in seen:
            plan.append(normalized)
            seen.add(key)
        if len(plan) == MAX_TOPICS:
            break
    return plan


class InterviewService:
    def __init__(self):
        self._video_futures: Dict[str, List[concurrent.futures.Future]] = defaultdict(list)

    def _run_background_video_analysis(self, session_id: str, question_id: int, rec_path: Path):
        """Runs video analysis in background thread and writes ONLY video_metrics via targeted update."""
        try:
            metrics = video_service.analyze_video_recording(rec_path)
            # Step E: targeted update — never call get_session/save_session from background
            ok = session_storage.update_video_metrics(session_id, question_id, metrics)
            if ok:
                logger.info(f"Background video analysis done session={session_id} q={question_id} method={metrics.analysis_method}")
            else:
                logger.info(f"Background video metrics buffered (pending) session={session_id} q={question_id}")
        except Exception as e:
            logger.error(f"Background video analysis failed session={session_id} q={question_id}: {e}", exc_info=True)

    def create_interview_session(
        self,
        selected_role: str,
        filename: str,
        pdf_content: bytes,
        user_id: Optional[str] = None
    ) -> AssessmentSession:
        """Process resume PDF, generate Question 1, and initialize isolated session with user ownership."""
        session_id = str(uuid4())
        
        # 1. Save uploaded resume file to disk
        file_path = file_storage.save_resume(session_id, filename, pdf_content)
        
        # 2. Extract structured resume JSON
        extracted_resume = resume_service.process_resume(file_path)
        
        # 3. Generate initial Question 1
        q1 = question_service.generate_initial_question(selected_role, extracted_resume)
        topic_plan = build_topic_plan(selected_role, extracted_resume, q1.topic or "Core Fundamentals")
        
        # 4. Create and persist session object
        session = AssessmentSession(
            session_id=session_id,
            user_id=user_id,
            assessment_type=AssessmentType.INTERVIEW,
            selected_role=selected_role.strip(),
            filename=filename,
            resume_data=extracted_resume,
            questions=[q1],
            submissions={},
            status="active",
            current_question_number=1,
            state_data={
                "candidate_skill_map": {
                    skill: {"estimated_level": "INTERMEDIATE", "confidence": 0.50}
                    for skill in extracted_resume.get("skills", [])[:5]
                },
                "topics_tested": [q1.topic],
                "strengths": [],
                "knowledge_gaps": [],
                "difficulty_progression": [q1.difficulty or "EASY"],
                "topic_plan": topic_plan,
                "topic_counts": {topic: (1 if topic.lower() == (q1.topic or "").lower() else 0) for topic in topic_plan},
                "current_topic_index": 0,
                "max_questions_per_topic": MAX_QUESTIONS_PER_TOPIC,
                "max_questions": MAX_TOPICS * MAX_QUESTIONS_PER_TOPIC
            }
        )
        session_storage.save_session(session)
        return session

    def process_answer_submission(
        self,
        session_id: str,
        question_id: int,
        user_id: Optional[str] = None,
        answer_text: Optional[str] = "",
        recording_bytes: Optional[bytes] = None
    ) -> Dict[str, Any]:
        """Full Adaptive Multimodal Answer Pipeline:
        WebM -> Storage -> Whisper STT -> Speech Metrics -> Video Posture -> Groq NLP -> Score -> Adaptive Engine -> Next Question
        """
        session = session_storage.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Interview session {session_id} not found.")

        # Validate User Ownership
        if user_id and session.user_id and session.user_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied: You do not own this interview session.")

        current_q = next((q for q in session.questions if q.id == question_id), None)
        if not current_q:
            raise HTTPException(status_code=404, detail=f"Question {question_id} not found in session.")

        submission = session.submissions.get(question_id) or AnswerSubmission(question_id=question_id)
        
        if answer_text:
            submission.answer_text = answer_text
            submission.transcript = answer_text

        if recording_bytes:
            # 1. Save WebM recording
            rec_path = file_storage.save_recording(session_id, question_id, recording_bytes, file_extension="webm")
            submission.video_path = str(rec_path)
            
            # 2. Extract audio & run Speech-to-Text via Groq Whisper
            audio_path = audio_service.extract_audio_from_webm(rec_path)
            submission.audio_path = str(audio_path)
            
            stt_transcript, duration, word_timestamps = speech_service.transcribe_audio(audio_path)
            
            if stt_transcript:
                submission.transcript = stt_transcript
                from backend.app.services.answer_quality import classify_answer
                if classify_answer(stt_transcript) == "no_speech":
                    submission.transcription_status = "empty"
                else:
                    submission.transcription_status = "ok"
            elif not submission.transcript and not submission.answer_text:
                # No STT result and no text fallback — record this explicitly
                submission.transcript = ""
                submission.transcription_status = "empty"
                logger.warning(f"Transcription returned empty for session={session_id} question={question_id}")
            else:
                # Use text answer as fallback only if explicitly provided by user
                submission.transcript = submission.answer_text or ""
                submission.transcription_status = "failed" if not stt_transcript else "ok"

            # 3. Speech Delivery Metrics (WPM, filler words, pause detection)
            submission.speech_metrics = speech_service.analyze_speech_delivery(
                transcript=submission.transcript or "",
                duration_seconds=duration,
                audio_path=audio_path,
                word_timestamps=word_timestamps
            )

            # 4. Offload OpenCV / MediaPipe Video Analysis to BACKGROUND thread pool
            submission.video_metrics = None
            future = _video_executor.submit(self._run_background_video_analysis, session_id, question_id, rec_path)
            self._video_futures[session_id].append(future)

        # 5. Single-Question NLP Technical Evaluation (fast)
        nlp_eval = nlp_service.evaluate_single_answer(
            job_title=session.selected_role,
            question=current_q,
            transcript=submission.transcript or "",
            resume_context=session.resume_data or {}
        )

        is_text_only = performance_service._is_text_only(submission)
        if is_text_only:
            sp_score = None
            vi_score = None
        else:
            sp_score = submission.speech_metrics.speech_score if submission.speech_metrics else None
            vi_score = submission.video_metrics.video_score if submission.video_metrics else None

        nlp_score = nlp_eval.get("combined_nlp_score", nlp_eval.get("overall_nlp_score", 80))

        # Calculate Per-Question Score (using available signals honestly)
        per_q_score = performance_service.calculate_per_question_score(
            nlp_score, sp_score, vi_score, text_only=is_text_only
        )
        submission.nlp_evaluation = nlp_eval

        session.submissions[question_id] = submission

        # Update candidate state tracking
        state_data = session.state_data or {}
        strengths = state_data.get("strengths", [])
        strengths.extend(nlp_eval.get("strengths", []))
        gaps = state_data.get("knowledge_gaps", [])
        gaps.extend(nlp_eval.get("knowledge_gaps", []))
        topics = state_data.get("topics_tested", [])
        if current_q.topic and current_q.topic not in topics:
            topics.append(current_q.topic)
        diff_prog = state_data.get("difficulty_progression", [])
        diff_prog.append(current_q.difficulty or "MEDIUM")

        topic_plan = state_data.get("topic_plan") or build_topic_plan(
            session.selected_role,
            session.resume_data or {},
            current_q.topic or "Core Fundamentals",
        )
        topic_counts = state_data.get("topic_counts") or {}
        current_topic_index = int(state_data.get("current_topic_index", 0))
        if current_topic_index >= len(topic_plan):
            current_topic_index = len(topic_plan) - 1

        current_topic = topic_plan[current_topic_index]
        topic_counts[current_topic] = int(topic_counts.get(current_topic, 0)) + 1

        if nlp_eval.get("major_misconception"):
            misconceptions = state_data.get("misconceptions_detected", [])
            misconceptions.append({
                "question_id": question_id,
                "question": current_q.question,
                "needs_followup": nlp_eval.get("needs_followup", True)
            })
            state_data["misconceptions_detected"] = misconceptions

        state_data["strengths"] = list(set(strengths))
        state_data["knowledge_gaps"] = list(set(gaps))
        state_data["topics_tested"] = topics
        state_data["difficulty_progression"] = diff_prog
        state_data["topic_plan"] = topic_plan
        state_data["topic_counts"] = topic_counts
        state_data["current_topic_index"] = current_topic_index
        session.state_data = state_data

        # 6. Adaptive Engine Decision for Next Question (returns immediately to client)
        next_question_item = None
        max_questions = MAX_TOPICS * MAX_QUESTIONS_PER_TOPIC
        satisfied_current_topic = nlp_score >= 80 or (
            nlp_score >= 65 and not nlp_eval.get("needs_followup", False)
        )

        if satisfied_current_topic or topic_counts[current_topic] >= MAX_QUESTIONS_PER_TOPIC:
            current_topic_index += 1
            state_data["current_topic_index"] = current_topic_index
            session.state_data = state_data

        has_topic_remaining = 0 <= current_topic_index < len(topic_plan)
        if question_id < max_questions and has_topic_remaining:
            decision = adaptive_engine.decide_next_question(
                session_id=session_id,
                role=session.selected_role,
                resume_context=session.resume_data or {},
                current_question={"id": current_q.id, "text": current_q.question, "topic": current_q.topic},
                transcript=submission.transcript or "",
                nlp_score=nlp_score,
                speech_score=sp_score or 0,
                video_score=vi_score or 0,
                technical_strengths=nlp_eval.get("strengths", []),
                knowledge_gaps=nlp_eval.get("knowledge_gaps", []),
                mentioned_topics=nlp_eval.get("topics_detected", []),
                difficulty_level=current_q.difficulty or "MEDIUM",
                question_number=question_id,
                topic_plan=topic_plan,
                topic_counts=topic_counts,
                current_topic_index=current_topic_index,
                needs_followup=bool(nlp_eval.get("needs_followup", False)),
                session_json=session.model_dump(mode="json")
            )

            target_topic = topic_plan[current_topic_index]
            if satisfied_current_topic or topic_counts[current_topic] >= MAX_QUESTIONS_PER_TOPIC:
                strategy = "NEW_TOPIC"
                reason = (
                    f"Completed {current_topic} and moved to {target_topic}."
                    if topic_counts[current_topic] >= MAX_QUESTIONS_PER_TOPIC
                    else f"Candidate demonstrated mastery of {current_topic}; moving to {target_topic}."
                )
            else:
                strategy = "REMEDIAL" if nlp_score < 65 else "FOLLOW_UP"
                reason = f"More assessment is needed in {target_topic} before moving to the next topic."

            next_q_id = question_id + 1
            next_question_item = QuestionItem(
                id=next_q_id,
                question=decision.next_question,
                topic=target_topic,
                difficulty=decision.next_difficulty,
                source=strategy.lower(),
                strategy=strategy,
                reason=reason
            )

            # Check if next question already added
            existing_next = next((q for q in session.questions if q.id == next_q_id), None)
            if existing_next:
                session.questions[next_q_id - 1] = next_question_item
            else:
                session.questions.append(next_question_item)

            session.current_question_number = next_q_id

        session_storage.save_session(session)

        return {
            "accepted": True,
            "session_id": session_id,
            "question_id": question_id,
            "transcript": submission.transcript,
            "per_question_score": per_q_score,
            "nlp_score": nlp_score,
            "speech_metrics": submission.speech_metrics.model_dump() if submission.speech_metrics else None,
            "video_metrics": submission.video_metrics.model_dump() if submission.video_metrics else None,
            "nlp_evaluation": nlp_eval,
            "is_last_question": (
                question_id >= max_questions
                or next_question_item is None
            ),
            "next_question": next_question_item.model_dump() if next_question_item else None
        }

    def evaluate_session(self, session_id: str, user_id: Optional[str] = None) -> AssessmentSession:
        """Generate final multimodal performance report and complete session."""
        session = session_storage.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Interview session {session_id} not found.")

        if user_id and session.user_id and session.user_id != user_id:
            raise HTTPException(status_code=403, detail="Access denied: You do not own this interview session.")

        # Wait for any running background video analysis tasks up to 8s (or take whatever is done)
        futures = self._video_futures.get(session_id, [])
        if futures:
            done, not_done = concurrent.futures.wait(futures, timeout=8.0)
            logger.info(f"evaluate_session: {len(done)} video analysis tasks finished, {len(not_done)} still pending.")
            fresh_session = session_storage.get_session(session_id)
            if fresh_session:
                session = fresh_session

        # Ensure any submission with video but missing video_metrics is marked unavailable
        for sub in session.submissions.values():
            if sub.video_path and sub.video_metrics is None:
                sub.video_metrics = video_service._unavailable_metrics("Video analysis pending or unavailable at evaluation time.")

        evaluation = performance_service.generate_multimodal_report(session)
        session.evaluation = evaluation
        session.status = "completed"
        session_storage.save_session(session)
        return session

interview_service = InterviewService()
