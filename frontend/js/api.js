const API = {
    getToken() {
        return localStorage.getItem("auth_token") || "";
    },

    setToken(token) {
        if (token) {
            localStorage.setItem("auth_token", token);
        } else {
            localStorage.removeItem("auth_token");
        }
    },

    getAuthHeaders(headers = {}) {
        const token = this.getToken();
        if (token) {
            headers["Authorization"] = "Bearer " + token;
        }
        return headers;
    },

    async register(name, email, password) {
        const response = await fetch("/api/auth/register", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, email, password })
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Registration failed.");
        }
        return await response.json();
    },

    async login(email, password) {
        const response = await fetch("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password })
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Login failed.");
        }
        const data = await response.json();
        if (data.access_token) {
            this.setToken(data.access_token);
        }
        return data;
    },

    async getCurrentUser() {
        const token = this.getToken();
        if (!token) return null;
        const response = await fetch("/api/auth/me", {
            headers: this.getAuthHeaders()
        });
        if (!response.ok) {
            this.setToken("");
            return null;
        }
        return await response.json();
    },

    async uploadResume(file, selectedRole) {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("selected_role", selectedRole);

        const response = await fetch("/api/resume/upload", {
            method: "POST",
            headers: this.getAuthHeaders(),
            body: formData
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Resume upload failed.");
        }
        return await response.json();
    },

    async getSession(sessionId) {
        const response = await fetch(`/api/interview/session/${encodeURIComponent(sessionId)}`, {
            headers: this.getAuthHeaders()
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Session not found.");
        }
        return await response.json();
    },

    async saveTextAnswer(sessionId, questionId, answerText) {
        const response = await fetch("/api/interview/answer", {
            method: "POST",
            headers: this.getAuthHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
                session_id: sessionId,
                question_id: questionId,
                answer: answerText
            })
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Could not save text answer.");
        }
        return await response.json();
    },

    async uploadRecording(sessionId, questionId, videoBlob, answerText = "") {
        const formData = new FormData();
        formData.append("question_id", questionId);
        if (videoBlob) {
            formData.append("video", videoBlob, `question_${questionId}.webm`);
        }
        if (answerText) {
            formData.append("answer", answerText);
        }

        const response = await fetch(`/api/interview/${encodeURIComponent(sessionId)}/recording`, {
            method: "POST",
            headers: this.getAuthHeaders(),
            body: formData
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Recording upload failed.");
        }
        return await response.json();
    },

    async evaluateInterview(sessionId) {
        const response = await fetch(`/api/interview/evaluate?session_id=${encodeURIComponent(sessionId)}`, {
            method: "POST",
            headers: this.getAuthHeaders()
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Evaluation failed.");
        }
        return await response.json();
    },

    async getJobRoles() {
        const response = await fetch("/api/interview/roles");
        if (!response.ok) {
            return [];
        }
        const data = await response.json();
        return data.roles || [];
    },

    async getUserHistory() {
        const response = await fetch("/api/interview/history", {
            headers: this.getAuthHeaders()
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Could not fetch interview history.");
        }
        const data = await response.json();
        return data.sessions || [];
    },

    async abandonSession(sessionId) {
        const response = await fetch(`/api/interview/${encodeURIComponent(sessionId)}/abandon`, {
            method: "POST",
            headers: this.getAuthHeaders()
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Could not abandon session.");
        }
        return await response.json();
    },

    async downloadReport(sessionId) {
        const response = await fetch(`/api/interview/${encodeURIComponent(sessionId)}/report`, {
            headers: this.getAuthHeaders()
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || "Report generation failed.");
        }
        return response; // caller handles blob download
    }
};

window.API = API;

