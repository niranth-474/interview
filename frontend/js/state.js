class AppState {
    constructor() {
        this.sessionId = new URLSearchParams(window.location.search).get("session_id") || "";
        this.selectedRole = new URLSearchParams(window.location.search).get("role") || "";
        this.questions = [];
        this.currentQuestionIndex = 0;
        this.answers = new Map(); // question_id -> answer/submission
        this.resumeData = null;
        this.isRecording = false;
        this.evaluation = null;
        this.maxQuestions = 16;
    }

    setSession(session) {
        this.sessionId = session.session_id;
        this.selectedRole = session.selected_role;
        this.questions = session.questions || [];
        this.maxQuestions = session.state_data?.max_questions || this.questions.length || 16;
        if (session.answers) {
            session.answers.forEach(ans => {
                this.answers.set(ans.question_id, ans);
            });
        }
        if (session.evaluation) {
            this.evaluation = session.evaluation;
        }
    }

    getCurrentQuestion() {
        return this.questions[this.currentQuestionIndex] || null;
    }

    isComplete() {
        return this.answers.size >= this.questions.length && this.questions.length > 0;
    }
}

window.state = new AppState();
