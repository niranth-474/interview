import json
from collections import defaultdict
from pathlib import Path
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from backend.app.core.config import settings
from backend.app.db.session import SessionLocal, init_db
from backend.app.db.models import (
    AssessmentSessionDB, QuestionDB, SubmissionDB, RecordingDB,
    SpeechAnalysisDB, VideoAnalysisDB, NLPEvaluationDB, FinalEvaluationDB
)
from backend.app.schemas.interview import AssessmentSession, AnswerSubmission
from backend.app.schemas.question import QuestionItem
from backend.app.schemas.evaluation import SpeechMetrics, VideoMetrics, MultimodalEvaluation, QuestionEvaluation
from backend.app.core.constants import AssessmentType

class SessionStorage:
    """Repository handling assessment session persistence via SQLAlchemy ORM (PostgreSQL/SQLite)."""

    def __init__(self, storage_dir: Path = settings.DATA_DIR):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Step E: pending video metrics for submissions not yet written to DB
        # {session_id: {question_id: VideoMetrics}}
        self._pending_video_metrics: Dict[str, Dict[int, Any]] = defaultdict(dict)
        try:
            init_db()
        except Exception as e:
            print(f"Warning: Database initialization error: {e}")

    def get_session(self, session_id: str) -> Optional[AssessmentSession]:
        """Fetch session from DB via session_id."""
        db: Session = SessionLocal()
        try:
            db_session = db.query(AssessmentSessionDB).filter(
                AssessmentSessionDB.session_id == session_id
            ).first()
            if db_session:
                return self._orm_to_domain(db_session)
        finally:
            db.close()

        # Fallback load from legacy JSON audit file if DB record missing
        json_file = self.storage_dir / f"{session_id}.json"
        if json_file.exists():
            try:
                raw_data = json.loads(json_file.read_text(encoding="utf-8"))
                return AssessmentSession(**raw_data)
            except Exception as e:
                print(f"JSON legacy load error for {session_id}: {e}")
        return None

    def get_user_sessions(self, user_id: str) -> List[dict]:
        """Fetch all assessment sessions belonging to a user, ordered by creation date desc."""
        db: Session = SessionLocal()
        try:
            db_sessions = db.query(AssessmentSessionDB).filter(
                AssessmentSessionDB.user_id == user_id
            ).order_by(AssessmentSessionDB.created_at.desc()).all()

            results = []
            for s in db_sessions:
                eval_data = None
                if s.evaluation:
                    eval_data = {
                        "overall_score": s.evaluation.overall_score,
                        "nlp_score": s.evaluation.nlp_score,
                        "speech_score": s.evaluation.speech_score,
                        "video_score": s.evaluation.video_score
                    }
                results.append({
                    "session_id": s.session_id,
                    "selected_role": s.selected_role,
                    "status": s.status,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "evaluation": eval_data
                })
            return results
        finally:
            db.close()

    # ── Step E: Targeted video update ───────────────────────────────────────────

    def update_video_metrics(self, session_id: str, question_id: int, metrics) -> bool:
        """Targeted update: set only video_metrics for (session_id, question_id).

        If the SubmissionDB row doesn't exist yet (race condition),
        store in pending dict to be merged when save_session() runs.
        """
        db: Session = SessionLocal()
        try:
            db_session = db.query(AssessmentSessionDB).filter(
                AssessmentSessionDB.session_id == session_id
            ).first()
            if not db_session:
                self._pending_video_metrics[session_id][question_id] = metrics
                return False

            db_sub = db.query(SubmissionDB).filter(
                SubmissionDB.session_id == db_session.id,
                SubmissionDB.question_id == question_id
            ).first()
            if not db_sub:
                self._pending_video_metrics[session_id][question_id] = metrics
                return False

            # Write only video_metrics — do NOT touch any other column
            db_sub.video_metrics = metrics.model_dump() if metrics else None

            # Upsert VideoAnalysisDB row
            db_vi = db.query(VideoAnalysisDB).filter(
                VideoAnalysisDB.session_id == db_session.id,
                VideoAnalysisDB.question_id == question_id
            ).first()
            if db_vi:
                db_vi.face_visibility_score = metrics.face_visibility_score
                db_vi.attention_score = metrics.attention_score
                db_vi.posture_score = metrics.posture_score
                db_vi.stability_score = metrics.stability_score
                db_vi.video_score = metrics.video_score
                db_vi.metrics_json = metrics.model_dump()
            else:
                db.add(VideoAnalysisDB(
                    session_id=db_session.id,
                    question_id=question_id,
                    face_visibility_score=metrics.face_visibility_score,
                    attention_score=metrics.attention_score,
                    posture_score=metrics.posture_score,
                    stability_score=metrics.stability_score,
                    video_score=metrics.video_score,
                    metrics_json=metrics.model_dump()
                ))

            db.commit()
            self._pending_video_metrics.get(session_id, {}).pop(question_id, None)
            return True
        except Exception as e:
            db.rollback()
            print(f"update_video_metrics error session={session_id} q={question_id}: {e}")
            return False
        finally:
            db.close()

    def save_session(self, session: AssessmentSession) -> bool:
        """Persist assessment session to Database & write JSON backup."""
        # 1. Write JSON file audit backup
        try:
            json_file = self.storage_dir / f"{session.session_id}.json"
            json_file.write_text(json.dumps(self._adaptive_json(session), indent=2), encoding="utf-8")
        except Exception as e:
            print(f"JSON backup save warning: {e}")

        # 2. Persist to Database (PostgreSQL / SQLite ORM)
        db: Session = SessionLocal()
        try:
            db_session = db.query(AssessmentSessionDB).filter(AssessmentSessionDB.session_id == session.session_id).first()
            if not db_session:
                db_session = AssessmentSessionDB(
                    session_id=session.session_id,
                    user_id=session.user_id,
                    assessment_type=session.assessment_type,
                    selected_role=session.selected_role,
                    filename=session.filename,
                    resume_data=session.resume_data,
                    status=session.status,
                    current_question_number=session.current_question_number,
                    state_data=session.state_data
                )
                db.add(db_session)
                db.flush()

            if session.user_id:
                db_session.user_id = session.user_id
            db_session.selected_role = session.selected_role
            db_session.filename = session.filename
            db_session.resume_data = session.resume_data
            db_session.status = session.status
            db_session.current_question_number = session.current_question_number
            db_session.state_data = session.state_data

            # ── Step D: Upsert Questions by (session_id, question_id) ─────────
            existing_qs = {
                q.question_id: q
                for q in db.query(QuestionDB).filter(QuestionDB.session_id == db_session.id).all()
            }
            for q in session.questions:
                if q.id in existing_qs:
                    db_q = existing_qs[q.id]
                    db_q.question_text = q.question
                    db_q.topic = q.topic
                    db_q.difficulty = q.difficulty or "MEDIUM"
                    db_q.source = q.source or "role"
                    db_q.strategy = q.strategy or "NEW_TOPIC"
                    db_q.reason = q.reason
                else:
                    db.add(QuestionDB(
                        session_id=db_session.id,
                        question_id=q.id,
                        question_text=q.question,
                        topic=q.topic,
                        difficulty=q.difficulty or "MEDIUM",
                        source=q.source or "role",
                        strategy=q.strategy or "NEW_TOPIC",
                        reason=q.reason
                    ))

            # ── Steps B/D: Upsert Submissions by (session_id, question_id) ────
            existing_subs = {
                s.question_id: s
                for s in db.query(SubmissionDB).filter(SubmissionDB.session_id == db_session.id).all()
            }
            for q_id, sub in session.submissions.items():
                # Step E: resolve effective video_metrics
                # Priority: in-memory non-None > pending > leave DB untouched
                pending_vm = self._pending_video_metrics.get(session.session_id, {}).get(q_id)
                if sub.video_metrics is not None:
                    effective_vm = sub.video_metrics
                elif pending_vm is not None:
                    effective_vm = pending_vm
                else:
                    effective_vm = None  # sentinel: do not overwrite DB

                if q_id in existing_subs:
                    db_sub = existing_subs[q_id]
                    db_sub.answer_text = sub.answer_text
                    db_sub.transcript = sub.transcript
                    db_sub.audio_path = sub.audio_path
                    db_sub.video_path = sub.video_path
                    # Step B: persist transcription_status and nlp_evaluation
                    db_sub.transcription_status = sub.transcription_status or "no_recording"
                    db_sub.nlp_evaluation = sub.nlp_evaluation
                    db_sub.speech_metrics = sub.speech_metrics.model_dump() if sub.speech_metrics else None
                    # Step E: only overwrite video_metrics when we have a real value
                    if effective_vm is not None:
                        db_sub.video_metrics = effective_vm.model_dump() if hasattr(effective_vm, "model_dump") else effective_vm
                    # else: leave DB video_metrics untouched
                else:
                    db_sub = SubmissionDB(
                        session_id=db_session.id,
                        question_id=q_id,
                        answer_text=sub.answer_text,
                        transcript=sub.transcript,
                        audio_path=sub.audio_path,
                        video_path=sub.video_path,
                        transcription_status=sub.transcription_status or "no_recording",
                        nlp_evaluation=sub.nlp_evaluation,
                        speech_metrics=sub.speech_metrics.model_dump() if sub.speech_metrics else None,
                        video_metrics=effective_vm.model_dump() if (effective_vm and hasattr(effective_vm, "model_dump")) else None
                    )
                    db.add(db_sub)

                if pending_vm is not None:
                    self._pending_video_metrics.get(session.session_id, {}).pop(q_id, None)

                # Sync granular recordings metadata if path present
                if sub.video_path:
                    db_rec = db.query(RecordingDB).filter(
                        RecordingDB.session_id == db_session.id,
                        RecordingDB.question_id == q_id
                    ).first()
                    if not db_rec:
                        db_rec = RecordingDB(
                            session_id=db_session.id,
                            question_id=q_id,
                            file_path=sub.video_path,
                            file_type="webm",
                            duration=sub.speech_metrics.duration_seconds if sub.speech_metrics else 0.0
                        )
                        db.add(db_rec)

                # Sync granular Speech Analysis
                if sub.speech_metrics:
                    db_sp = db.query(SpeechAnalysisDB).filter(
                        SpeechAnalysisDB.session_id == db_session.id,
                        SpeechAnalysisDB.question_id == q_id
                    ).first()
                    if not db_sp:
                        db_sp = SpeechAnalysisDB(
                            session_id=db_session.id,
                            question_id=q_id,
                            wpm=sub.speech_metrics.words_per_minute,
                            filler_count=sub.speech_metrics.filler_word_count,
                            filler_frequency=sub.speech_metrics.filler_frequency,
                            speech_score=sub.speech_metrics.speech_score,
                            metrics_json=sub.speech_metrics.model_dump()
                        )
                        db.add(db_sp)

                # Upsert VideoAnalysis row (only when we have real metrics)
                if effective_vm and hasattr(effective_vm, "model_dump"):
                    db_vi = db.query(VideoAnalysisDB).filter(
                        VideoAnalysisDB.session_id == db_session.id,
                        VideoAnalysisDB.question_id == q_id
                    ).first()
                    if db_vi:
                        db_vi.face_visibility_score = effective_vm.face_visibility_score
                        db_vi.attention_score = effective_vm.attention_score
                        db_vi.posture_score = effective_vm.posture_score
                        db_vi.stability_score = effective_vm.stability_score
                        db_vi.video_score = effective_vm.video_score
                        db_vi.metrics_json = effective_vm.model_dump()
                    else:
                        db.add(VideoAnalysisDB(
                            session_id=db_session.id,
                            question_id=q_id,
                            face_visibility_score=effective_vm.face_visibility_score,
                            attention_score=effective_vm.attention_score,
                            posture_score=effective_vm.posture_score,
                            stability_score=effective_vm.stability_score,
                            video_score=effective_vm.video_score,
                            metrics_json=effective_vm.model_dump()
                        ))

            # Sync Evaluation
            if session.evaluation:
                db.query(FinalEvaluationDB).filter(FinalEvaluationDB.session_id == db_session.id).delete()
                eval_obj = session.evaluation
                db_eval = FinalEvaluationDB(
                    session_id=db_session.id,
                    overall_score=eval_obj.overall_score,
                    nlp_score=eval_obj.nlp_score,
                    speech_score=eval_obj.speech_score,
                    video_score=eval_obj.video_score,
                    summary=eval_obj.summary,
                    strengths=eval_obj.strengths,
                    gaps=eval_obj.gaps,
                    recommendations=eval_obj.recommendations,
                    per_question=[pq.model_dump() for pq in eval_obj.per_question]
                )
                db.add(db_eval)

            db.commit()
            return True
        except Exception as e:
            db.rollback()
            print(f"DB session save error: {e}")
            return False
        finally:
            db.close()

    def _adaptive_json(self, session: AssessmentSession) -> dict:
        data = session.model_dump(mode="json")
        state = session.state_data or {}
        topic_plan = state.get("topic_plan") or []
        topic_counts = state.get("topic_counts") or {}
        current_index = int(state.get("current_topic_index", 0))
        if current_index >= len(topic_plan):
            current_index = max(len(topic_plan) - 1, 0)

        submissions = session.submissions
        topic_types = state.get("topic_types", {})
        job_role_topics = [topic for topic in topic_plan[:2] if topic]
        project_topic_names = [
            project.get("name")
            for project in (session.resume_data or {}).get("projects", [])
            if isinstance(project, dict) and project.get("name")
        ]

        if topic_types:
            grouped_plan = {"JOB_ROLE": [], "TECHNICAL_SKILLS": [], "PROJECT": []}
            for topic in topic_plan:
                grouped_plan.setdefault(topic_types.get(topic, "TECHNICAL_SKILLS"), []).append(topic)
        else:
            grouped_plan = {
                "JOB_ROLE": job_role_topics,
                "TECHNICAL_SKILLS": [topic for topic in topic_plan if topic not in job_role_topics and topic not in project_topic_names][:4],
                "PROJECT": [topic for topic in topic_plan if topic in project_topic_names],
            }

        grouped_questions = {topic: [] for topic in topic_plan}
        for question in session.questions:
            topic = question.topic or "General"
            grouped_questions.setdefault(topic, [])
            submission = submissions.get(question.id)
            if submission is None:
                submission = submissions.get(str(question.id))

            nlp = submission.nlp_evaluation if submission else {}
            answer = ""
            if submission:
                answer = submission.answer_text or submission.transcript or ""

            history_question = {
                "id": question.id,
                "difficulty": question.difficulty,
                "question": question.question,
                "answer": answer,
                "nlp_score": nlp.get("combined_nlp_score", nlp.get("overall_nlp_score", 0)),
                "knowledge_gaps": nlp.get("knowledge_gaps", []),
                "needs_followup": bool(nlp.get("needs_followup", False)),
            }

            if submission:
                history_question.update({
                    "answer_text": submission.answer_text or "",
                    "transcript": submission.transcript or "",
                    "transcription_status": submission.transcription_status,
                    "audio_path": submission.audio_path,
                    "video_path": submission.video_path,
                    "speech_metrics": submission.speech_metrics.model_dump(mode="json") if submission.speech_metrics else None,
                    "video_metrics": submission.video_metrics.model_dump(mode="json") if submission.video_metrics else None,
                    "nlp_evaluation": nlp,
                })

            grouped_questions[topic].append(history_question)

        history = []
        for index, topic in enumerate(topic_plan):
            questions = grouped_questions.get(topic, [])
            count = topic_counts.get(topic, len(questions))
            if count >= state.get("max_questions_per_topic", 3) or index < current_index:
                status = "complete"
            elif index == current_index or questions:
                status = "in_progress"
            else:
                status = "pending"
            history.append({
                "topic_number": index + 1,
                "topic": topic,
                "topic_type": topic_types.get(topic, "JOB_ROLE" if topic in job_role_topics else "PROJECT" if topic in project_topic_names else "TECHNICAL_SKILLS"),
                "status": status,
                "question_count": count,
                "questions": questions,
            })

        data["max_topics"] = state.get("max_topics", 6)
        data["current_topic_number"] = current_index + 1 if topic_plan else 1
        data["current_question_number"] = session.current_question_number
        data["topic_plan"] = grouped_plan
        data["topic_history"] = history
        data["resume_summary"] = {
            "skills": (session.resume_data or {}).get("skills", []),
            "projects": [
                {
                    "name": project.get("name"),
                    "tech_stack": project.get("tech_stack", []),
                }
                for project in (session.resume_data or {}).get("projects", [])
                if isinstance(project, dict)
            ],
        }
        return data

    def _orm_to_domain(self, db_session: AssessmentSessionDB) -> AssessmentSession:
        """Convert SQLAlchemy ORM DB object into domain AssessmentSession."""
        questions = [
            QuestionItem(
                id=q.question_id,
                question=q.question_text,
                topic=q.topic,
                difficulty=q.difficulty or "MEDIUM",
                source=q.source or "role",
                strategy=q.strategy or "NEW_TOPIC",
                reason=q.reason
            )
            for q in sorted(db_session.questions, key=lambda x: x.question_id)
        ]

        submissions = {}
        for sub in db_session.submissions:
            # Step C: return None for missing metrics — NEVER SpeechMetrics() / VideoMetrics() defaults
            sp_metrics = SpeechMetrics(**sub.speech_metrics) if sub.speech_metrics else None
            vi_metrics = VideoMetrics(**sub.video_metrics) if sub.video_metrics else None

            submissions[sub.question_id] = AnswerSubmission(
                question_id=sub.question_id,
                answer_text=sub.answer_text,
                transcript=sub.transcript,
                audio_path=sub.audio_path,
                video_path=sub.video_path,
                # Step B: load transcription_status and nlp_evaluation from DB
                transcription_status=sub.transcription_status or "no_recording",
                nlp_evaluation=sub.nlp_evaluation or None,
                speech_metrics=sp_metrics,
                video_metrics=vi_metrics,
            )

        evaluation = None
        if db_session.evaluation:
            e = db_session.evaluation
            pq_list = [QuestionEvaluation(**item) for item in (e.per_question or [])]
            modalities = []
            if e.nlp_score is not None:
                modalities.append("nlp")
            if e.speech_score is not None:
                modalities.append("speech")
            if e.video_score is not None:
                modalities.append("video")

            evaluation = MultimodalEvaluation(
                overall_score=e.overall_score,
                nlp_score=e.nlp_score,
                speech_score=e.speech_score,
                video_score=e.video_score,
                modalities_used=modalities,
                summary=e.summary or "",
                strengths=e.strengths or [],
                gaps=e.gaps or [],
                recommendations=e.recommendations or [],
                per_question=pq_list
            )

        return AssessmentSession(
            session_id=db_session.session_id,
            user_id=db_session.user_id,
            assessment_type=AssessmentType(db_session.assessment_type) if db_session.assessment_type in AssessmentType.__members__ else AssessmentType.INTERVIEW,
            selected_role=db_session.selected_role,
            filename=db_session.filename,
            resume_data=db_session.resume_data,
            questions=questions,
            submissions=submissions,
            evaluation=evaluation,
            status=db_session.status,
            current_question_number=db_session.current_question_number or 1,
            state_data=db_session.state_data or {},
            created_at=db_session.created_at.isoformat() if db_session.created_at else None
        )

session_storage = SessionStorage()
