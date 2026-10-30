/**
 * interview.js — Continuous Recording Flow
 * Keeps camera/mic active across all questions.
 * Each "Next Question" stops current question clip, immediately starts
 * recording the next clip on the same stream, and uploads the previous clip.
 */

let timerInterval = null;
let timerSeconds = 0;
let isSubmitting = false;
let interviewEnded = false;
let pendingUpload = null; // Holds { blob, textAnswer, questionId, isLast } in case of retry

document.addEventListener("DOMContentLoaded", async () => {
    const videoElem        = document.getElementById("webcamVideo");
    const recBadge         = document.getElementById("recordingBadge");
    const timerElem        = document.getElementById("recordingTimer");
    const nextQuestionBtn  = document.getElementById("nextQuestionBtn");
    const textAnswerInput  = document.getElementById("textAnswerInput");
    const transcriptBox    = document.getElementById("transcriptBox");
    const statusMessage    = document.getElementById("statusMessage");
    const quitBtn          = document.getElementById("quitInterviewBtn");
    const quitModal        = document.getElementById("quitConfirmModal");
    const quitConfirmOk    = document.getElementById("quitConfirmOk");
    const quitConfirmClose = document.getElementById("quitConfirmClose");
    const completionBanner = document.getElementById("completionBanner");
    const questionText     = document.getElementById("questionText");
    const questionLoader   = document.getElementById("questionLoader");
    const uploadRetryBox   = document.getElementById("uploadRetryBox");
    const uploadRetryMsg   = document.getElementById("uploadRetryMsg");
    const retryUploadBtn   = document.getElementById("retryUploadBtn");

    // ── Voice Controls Elements ─────────────────────────────────────────────
    const voiceControlsPanel = document.getElementById("voiceControlsPanel");
    const voicePlayBtn       = document.getElementById("voicePlayBtn");
    const voicePlayText      = document.getElementById("voicePlayText");
    const voicePauseBtn      = document.getElementById("voicePauseBtn");
    const voicePauseText     = document.getElementById("voicePauseText");
    const voicePauseIcon     = document.getElementById("voicePauseIcon");
    const voiceMuteBtn       = document.getElementById("voiceMuteBtn");
    const voiceMuteText      = document.getElementById("voiceMuteText");
    const voiceMuteIcon      = document.getElementById("voiceMuteIcon");
    const voiceWaveform      = document.getElementById("voiceWaveform");
    const voiceStatusBadge   = document.getElementById("voiceStatusBadge");

    // ── Speech Synthesis State ──────────────────────────────────────────────
    const isSpeechSupported = ("speechSynthesis" in window) && ("SpeechSynthesisUtterance" in window);
    const TTS_MUTE_KEY = "interview_voice_muted";
    let isVoiceMuted = false;
    try {
        isVoiceMuted = localStorage.getItem(TTS_MUTE_KEY) === "true";
    } catch (e) {
        console.warn("localStorage inaccessible for voice mute setting:", e);
    }

    let speechState = "idle"; // "idle" | "speaking" | "paused"
    let currentUtterance = null;
    let lastAutoSpokenQuestionId = null;
    let speechKeepAliveTimer = null;
    let isQuestionLoading = false;

    const SVG_ICONS = {
        pause: '<rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect>',
        play: '<polygon points="5 3 19 12 5 21 5 3"></polygon>',
        volumeOn: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"></path>',
        volumeMute: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="23" y1="9" x2="17" y2="15"></line><line x1="17" y1="9" x2="23" y2="15"></line>'
    };

    // ── Voice Selection Helper ──────────────────────────────────────────────
    function findBestEnglishVoice() {
        if (!isSpeechSupported) return null;
        const voices = window.speechSynthesis.getVoices() || [];
        if (voices.length === 0) return null;

        // 1. Prefer English (India): en-IN, en_IN, or name containing India
        const inVoice = voices.find(v => {
            const lang = (v.lang || "").toLowerCase().replace("_", "-");
            const name = (v.name || "").toLowerCase();
            return lang === "en-in" || lang.startsWith("en-in") || (lang.startsWith("en") && name.includes("india"));
        });
        if (inVoice) return inVoice;

        // 2. High-quality / natural English voice fallback
        const enVoices = voices.filter(v => (v.lang || "").toLowerCase().startsWith("en"));
        if (enVoices.length > 0) {
            const naturalVoice = enVoices.find(v => {
                const name = (v.name || "").toLowerCase();
                return name.includes("natural") || name.includes("online") || name.includes("google") || name.includes("neural") || name.includes("aria");
            });
            return naturalVoice || enVoices[0];
        }

        // 3. System default or first available voice
        return voices.find(v => v.default) || voices[0] || null;
    }

    if (isSpeechSupported && window.speechSynthesis.onvoiceschanged !== undefined) {
        window.speechSynthesis.onvoiceschanged = () => {
            findBestEnglishVoice();
        };
    }

    // ── Voice UI Synchronization ───────────────────────────────────────────
    function updateVoiceUI() {
        if (!voiceControlsPanel) return;

        if (!isSpeechSupported) {
            if (voicePlayBtn) voicePlayBtn.disabled = true;
            if (voicePauseBtn) voicePauseBtn.disabled = true;
            if (voiceMuteBtn) voiceMuteBtn.disabled = true;
            if (voiceStatusBadge) {
                voiceStatusBadge.textContent = "Speech Unavailable";
                voiceStatusBadge.className = "voice-status-badge status-muted";
            }
            if (voiceWaveform) voiceWaveform.className = "voice-waveform";
            return;
        }

        if (interviewEnded) {
            if (voicePlayBtn) voicePlayBtn.disabled = true;
            if (voicePauseBtn) voicePauseBtn.disabled = true;
            if (voiceMuteBtn) voiceMuteBtn.disabled = true;
            if (voiceStatusBadge) {
                voiceStatusBadge.textContent = "Interview Ended";
                voiceStatusBadge.className = "voice-status-badge status-muted";
            }
            if (voiceWaveform) voiceWaveform.className = "voice-waveform";
            return;
        }

        if (isQuestionLoading) {
            if (voicePlayBtn) voicePlayBtn.disabled = true;
            if (voicePauseBtn) voicePauseBtn.disabled = true;
            if (voiceStatusBadge) {
                voiceStatusBadge.textContent = "Preparing Question…";
                voiceStatusBadge.className = "voice-status-badge";
            }
            if (voiceWaveform) voiceWaveform.className = "voice-waveform";
            return;
        }

        // Listen / Replay button
        if (voicePlayBtn) {
            voicePlayBtn.disabled = false;
            if (voicePlayText) {
                voicePlayText.textContent = (speechState === "speaking") ? "Replay Question" : "Listen to Question";
            }
        }

        // Pause / Resume button
        if (voicePauseBtn) {
            if (speechState === "speaking") {
                voicePauseBtn.disabled = false;
                if (voicePauseText) voicePauseText.textContent = "Pause";
                if (voicePauseIcon) voicePauseIcon.innerHTML = SVG_ICONS.pause;
                voicePauseBtn.setAttribute("aria-label", "Pause interviewer voice");
                voicePauseBtn.title = "Pause speech";
            } else if (speechState === "paused") {
                voicePauseBtn.disabled = false;
                if (voicePauseText) voicePauseText.textContent = "Resume";
                if (voicePauseIcon) voicePauseIcon.innerHTML = SVG_ICONS.play;
                voicePauseBtn.setAttribute("aria-label", "Resume interviewer voice");
                voicePauseBtn.title = "Resume speech";
            } else {
                voicePauseBtn.disabled = true;
                if (voicePauseText) voicePauseText.textContent = "Pause";
                if (voicePauseIcon) voicePauseIcon.innerHTML = SVG_ICONS.pause;
                voicePauseBtn.setAttribute("aria-label", "Pause interviewer voice");
                voicePauseBtn.title = "Pause speech";
            }
        }

        // Mute / Unmute button
        if (voiceMuteBtn) {
            voiceMuteBtn.disabled = false;
            if (isVoiceMuted) {
                voiceMuteBtn.classList.add("active-mute");
                if (voiceMuteText) voiceMuteText.textContent = "Unmute";
                if (voiceMuteIcon) voiceMuteIcon.innerHTML = SVG_ICONS.volumeMute;
                voiceMuteBtn.setAttribute("aria-label", "Unmute automatic interviewer voice");
                voiceMuteBtn.title = "Enable automatic voice for questions";
            } else {
                voiceMuteBtn.classList.remove("active-mute");
                if (voiceMuteText) voiceMuteText.textContent = "Mute";
                if (voiceMuteIcon) voiceMuteIcon.innerHTML = SVG_ICONS.volumeOn;
                voiceMuteBtn.setAttribute("aria-label", "Mute automatic interviewer voice");
                voiceMuteBtn.title = "Disable automatic voice for questions";
            }
        }

        // Container classes & visual waveform
        voiceControlsPanel.classList.toggle("speaking", speechState === "speaking");
        voiceControlsPanel.classList.toggle("paused", speechState === "paused");

        if (voiceWaveform) {
            voiceWaveform.className = "voice-waveform" +
                (speechState === "speaking" ? " speaking" : (speechState === "paused" ? " paused" : ""));
        }

        // Status badge label
        if (voiceStatusBadge) {
            if (speechState === "speaking") {
                voiceStatusBadge.textContent = "Interviewer Speaking…";
                voiceStatusBadge.className = "voice-status-badge status-speaking";
            } else if (speechState === "paused") {
                voiceStatusBadge.textContent = "Voice Paused";
                voiceStatusBadge.className = "voice-status-badge status-paused";
            } else if (isVoiceMuted) {
                voiceStatusBadge.textContent = "Auto Voice Off";
                voiceStatusBadge.className = "voice-status-badge status-muted";
            } else {
                voiceStatusBadge.textContent = "AI Voice Ready";
                voiceStatusBadge.className = "voice-status-badge";
            }
        }
    }

    // ── Speech Playback Control ─────────────────────────────────────────────
    function stopSpeech() {
        if (speechKeepAliveTimer) {
            clearInterval(speechKeepAliveTimer);
            speechKeepAliveTimer = null;
        }
        if (isSpeechSupported) {
            try {
                window.speechSynthesis.cancel();
            } catch (e) {
                console.warn("speechSynthesis.cancel error:", e);
            }
        }
        currentUtterance = null;
        speechState = "idle";
        updateVoiceUI();
    }

    function speakQuestion(text, questionId, isAuto = false) {
        if (!isSpeechSupported || interviewEnded || !text) return;

        // If automatic speech requested but candidate is muted, skip auto-speak
        if (isAuto && isVoiceMuted) {
            updateVoiceUI();
            return;
        }

        // Guard against duplicate auto-reads of the same question
        if (isAuto && lastAutoSpokenQuestionId === questionId) {
            return;
        }

        if (isAuto) {
            lastAutoSpokenQuestionId = questionId;
        }

        // Cancel previous speech before speaking anew
        stopSpeech();

        const cleanText = String(text).trim();
        if (!cleanText) return;

        const utterance = new SpeechSynthesisUtterance(cleanText);
        const voice = findBestEnglishVoice();
        if (voice) {
            utterance.voice = voice;
            utterance.lang = voice.lang;
        } else {
            utterance.lang = "en-IN";
        }

        utterance.rate = 0.95; // Natural speaking rate around 0.9 - 1.0
        utterance.pitch = 1.0;
        utterance.volume = 1.0;

        utterance.onstart = () => {
            if (currentUtterance === utterance) {
                speechState = "speaking";
                updateVoiceUI();
            }
        };

        utterance.onpause = () => {
            if (currentUtterance === utterance) {
                speechState = "paused";
                updateVoiceUI();
            }
        };

        utterance.onresume = () => {
            if (currentUtterance === utterance) {
                speechState = "speaking";
                updateVoiceUI();
            }
        };

        utterance.onend = () => {
            if (currentUtterance === utterance) {
                if (speechKeepAliveTimer) {
                    clearInterval(speechKeepAliveTimer);
                    speechKeepAliveTimer = null;
                }
                speechState = "idle";
                currentUtterance = null;
                updateVoiceUI();
            }
        };

        utterance.onerror = (event) => {
            console.warn("SpeechSynthesis utterance notice:", event.error);
            if (currentUtterance === utterance) {
                if (speechKeepAliveTimer) {
                    clearInterval(speechKeepAliveTimer);
                    speechKeepAliveTimer = null;
                }
                speechState = "idle";
                currentUtterance = null;
                if (event.error === "not-allowed") {
                    if (voiceStatusBadge) {
                        voiceStatusBadge.textContent = "Click Listen to hear";
                        voiceStatusBadge.className = "voice-status-badge status-muted";
                    }
                } else {
                    updateVoiceUI();
                }
            }
        };

        currentUtterance = utterance;
        speechState = "speaking";
        updateVoiceUI();

        // Browser keep-alive: prevent silent pause on long speech utterances
        speechKeepAliveTimer = setInterval(() => {
            if (window.speechSynthesis && window.speechSynthesis.speaking && speechState === "speaking") {
                if (window.speechSynthesis.paused) {
                    window.speechSynthesis.resume();
                }
            } else {
                clearInterval(speechKeepAliveTimer);
                speechKeepAliveTimer = null;
            }
        }, 5000);

        try {
            window.speechSynthesis.speak(utterance);
        } catch (err) {
            console.error("speechSynthesis.speak error:", err);
            speechState = "idle";
            currentUtterance = null;
            updateVoiceUI();
        }
    }

    // ── Attach Voice Controls Event Listeners ──────────────────────────────
    if (voicePlayBtn) {
        voicePlayBtn.addEventListener("click", () => {
            const q = window.state.getCurrentQuestion();
            if (q && q.question) {
                // Explicit user gesture: replay current question
                speakQuestion(q.question, q.id, false /* isAuto = false */);
            }
        });
    }

    if (voicePauseBtn) {
        voicePauseBtn.addEventListener("click", () => {
            if (!isSpeechSupported) return;
            if (speechState === "speaking") {
                try {
                    window.speechSynthesis.pause();
                } catch (e) {
                    console.warn("speechSynthesis.pause error:", e);
                }
                speechState = "paused";
                updateVoiceUI();
            } else if (speechState === "paused") {
                try {
                    window.speechSynthesis.resume();
                } catch (e) {
                    console.warn("speechSynthesis.resume error:", e);
                }
                speechState = "speaking";
                updateVoiceUI();
            }
        });
    }

    if (voiceMuteBtn) {
        voiceMuteBtn.addEventListener("click", () => {
            isVoiceMuted = !isVoiceMuted;
            try {
                localStorage.setItem(TTS_MUTE_KEY, isVoiceMuted ? "true" : "false");
            } catch (e) {
                console.warn("Cannot store mute state in localStorage:", e);
            }
            if (isVoiceMuted && speechState !== "idle") {
                stopSpeech();
            } else {
                updateVoiceUI();
            }
        });
    }

    // Initialize voice UI state on page load
    updateVoiceUI();

    // ── Auth guard ──────────────────────────────────────────────────────────
    if (window.Auth) {
        const user = await Auth.requireAuth("/login");
        if (!user) return;
    }

    if (!window.state || !window.state.sessionId) {
        if (statusMessage) statusMessage.textContent = "Session ID missing. Please upload resume first.";
        return;
    }

    // ── Load session ─────────────────────────────────────────────────────────
    try {
        const session = await API.getSession(window.state.sessionId);
        window.state.setSession(session);
        renderCurrentQuestion();
    } catch (err) {
        if (statusMessage) statusMessage.textContent = err.message || "Failed to load session.";
        return;
    }

    // ── Start camera preview & auto-start continuous recording ──────────────
    if (videoElem) {
        const camReady = await window.cameraManager.startCamera(videoElem);
        if (camReady) {
            startQuestionRecording();
        } else {
            if (statusMessage) {
                statusMessage.innerHTML = "<span style='color:var(--status-amber, #f59e0b)'>Camera unavailable. Text answers will be used for evaluation.</span>";
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // UI Helpers
    // ─────────────────────────────────────────────────────────────────────────

    function renderCurrentQuestion() {
        const q = window.state.getCurrentQuestion();
        if (!q) return;

        const topicElem = document.getElementById("questionTopic");
        if (topicElem) topicElem.textContent = `${q.topic || "Technical"} • Difficulty: ${q.difficulty || "MEDIUM"}`;

        const numberElem = document.getElementById("questionNumber");
        if (numberElem) numberElem.textContent = `Question ${window.state.currentQuestionIndex + 1}`;

        if (questionText) {
            questionText.textContent = q.question;
            questionText.style.display = "block";
        }
        if (questionLoader) {
            questionLoader.style.display = "none";
        }

        const fillElem = document.getElementById("progressBarFill");
        if (fillElem) {
            const pct = Math.min(100, ((window.state.currentQuestionIndex + 1) / window.state.maxQuestions) * 100);
            fillElem.style.width = `${pct}%`;
        }

        // Restore already-answered transcript if revisiting
        const existingAns = window.state.answers.get(q.id);
        if (existingAns) {
            if (textAnswerInput) textAnswerInput.value = existingAns.answer || "";
            if (transcriptBox) {
                transcriptBox.textContent = existingAns.answer ? `Transcript: ${existingAns.answer}` : "";
                transcriptBox.hidden = !existingAns.answer;
            }
        } else {
            if (textAnswerInput) textAnswerInput.value = "";
            if (transcriptBox) transcriptBox.hidden = true;
        }

        updateNextButtonLabel();

        // Auto-speak question when displayed according to interview flow
        if (q && q.question) {
            speakQuestion(q.question, q.id, true /* isAuto = true */);
        }
    }

    function updateNextButtonLabel() {
        if (!nextQuestionBtn) return;
        const isLast = (window.state.currentQuestionIndex >= 4);
        nextQuestionBtn.textContent = isLast ? "Finish Interview →" : "Next Question →";
    }

    function showQuestionLoading(loading) {
        isQuestionLoading = loading;
        if (questionLoader) questionLoader.style.display = loading ? "flex" : "none";
        if (questionText) questionText.style.display = loading ? "none" : "block";
        if (loading) {
            stopSpeech();
        }
        updateVoiceUI();
    }

    function startTimer() {
        stopTimer();
        timerSeconds = 0;
        if (timerElem) timerElem.textContent = "00:00";
        timerInterval = setInterval(() => {
            timerSeconds++;
            const mins = String(Math.floor(timerSeconds / 60)).padStart(2, "0");
            const secs = String(timerSeconds % 60).padStart(2, "0");
            if (timerElem) timerElem.textContent = `${mins}:${secs}`;
        }, 1000);
    }

    function stopTimer() {
        if (timerInterval) {
            clearInterval(timerInterval);
            timerInterval = null;
        }
    }

    function startQuestionRecording() {
        if (interviewEnded) return;
        const started = window.cameraManager.startRecording();
        if (started) {
            if (recBadge) recBadge.hidden = false;
            startTimer();
            if (statusMessage) {
                statusMessage.innerHTML = `<span style="color:var(--accent-cyan)">● Recording Answer ${window.state.currentQuestionIndex + 1}</span>`;
            }
        }
    }

    function showCompletionState() {
        interviewEnded = true;
        stopSpeech();
        stopTimer();
        window.cameraManager.cleanup();

        if (recBadge) recBadge.hidden = true;
        if (nextQuestionBtn) {
            nextQuestionBtn.disabled = true;
            nextQuestionBtn.textContent = "Interview Completed";
        }
        if (quitBtn) quitBtn.style.display = "none";
        if (uploadRetryBox) uploadRetryBox.style.display = "none";

        if (completionBanner) {
            completionBanner.hidden = false;
            completionBanner.scrollIntoView({ behavior: "smooth" });
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Answer Submission & Next Flow
    // ─────────────────────────────────────────────────────────────────────────

    async function submitAnswerFlow(dataToUpload) {
        const { blob, textAnswer, questionId, isLast } = dataToUpload;

        if (uploadRetryBox) uploadRetryBox.style.display = "none";

        try {
            // Upload recording + optional text answer
            const res = await API.uploadRecording(window.state.sessionId, questionId, blob, textAnswer);

            // Record transcript / answer
            window.state.answers.set(questionId, {
                question_id: questionId,
                answer: res.transcript || textAnswer || ""
            });

            // Clear pending retry data on success
            pendingUpload = null;

            if (isLast || res.is_last_question) {
                showCompletionState();
                if (statusMessage) statusMessage.textContent = "Generating your multimodal evaluation report…";
                try {
                    await API.evaluateInterview(window.state.sessionId);
                } catch (e) {
                    console.warn("Evaluation completed with notice:", e);
                }
                setTimeout(() => {
                    window.location.href = `/results?session_id=${encodeURIComponent(window.state.sessionId)}`;
                }, 1800);
            } else {
                // Not last question: advance to next question
                if (res.next_question) {
                    const existingIdx = window.state.questions.findIndex(q => q.id === res.next_question.id);
                    if (existingIdx >= 0) {
                        window.state.questions[existingIdx] = res.next_question;
                    } else {
                        window.state.questions.push(res.next_question);
                    }
                    window.state.currentQuestionIndex = window.state.questions.length - 1;
                } else {
                    window.state.currentQuestionIndex = Math.min(window.state.questions.length - 1, window.state.currentQuestionIndex + 1);
                }

                showQuestionLoading(false);
                renderCurrentQuestion();

                // Start recording only when next question is displayed
                startQuestionRecording();

                if (nextQuestionBtn) {
                    nextQuestionBtn.disabled = false;
                    updateNextButtonLabel();
                }
            }
        } catch (err) {
            console.error("Submission failed:", err);
            // Save upload data so user can retry without losing their recording
            pendingUpload = dataToUpload;
            if (uploadRetryMsg) uploadRetryMsg.textContent = `Upload error: ${err.message || "Failed to submit recording."}`;
            if (uploadRetryBox) uploadRetryBox.style.display = "block";
            if (nextQuestionBtn) {
                nextQuestionBtn.disabled = false;
                nextQuestionBtn.textContent = "Retry Submitting Answer";
            }
            showQuestionLoading(false);
            if (questionText) questionText.style.display = "block";
        } finally {
            isSubmitting = false;
        }
    }

    if (nextQuestionBtn) {
        nextQuestionBtn.addEventListener("click", async () => {
            if (isSubmitting || interviewEnded) return;

            // Stop any active question speech immediately when candidate advances
            stopSpeech();

            // If we have a pending failed upload, clicking here retries it
            if (pendingUpload) {
                isSubmitting = true;
                nextQuestionBtn.disabled = true;
                nextQuestionBtn.textContent = "Retrying upload…";
                await submitAnswerFlow(pendingUpload);
                return;
            }

            const currentQ = window.state.getCurrentQuestion();
            if (!currentQ) return;

            const isLast = window.state.currentQuestionIndex >= window.state.maxQuestions - 1
                || currentQ.id >= window.state.maxQuestions;
            const textAnswer = textAnswerInput ? textAnswerInput.value.trim() : "";

            isSubmitting = true;
            nextQuestionBtn.disabled = true;
            nextQuestionBtn.textContent = isLast ? "Finishing interview…" : "Preparing next question…";

            // 1. Stop current question's recorder to get its WebM Blob
            stopTimer();
            if (recBadge) recBadge.hidden = true;
            let blob = null;
            if (window.cameraManager && window.cameraManager.isRecording) {
                blob = await window.cameraManager.stopRecording();
            }

            const dataToUpload = {
                blob: blob,
                textAnswer: textAnswer,
                questionId: currentQ.id,
                isLast: isLast
            };

            // 2. While waiting for next question, show loader and keep live preview
            if (!isLast) {
                showQuestionLoading(true);
            }

            // 3. Upload current clip in parallel/background
            await submitAnswerFlow(dataToUpload);
        });
    }

    if (retryUploadBtn) {
        retryUploadBtn.addEventListener("click", async () => {
            if (pendingUpload && !isSubmitting) {
                stopSpeech();
                isSubmitting = true;
                if (nextQuestionBtn) nextQuestionBtn.disabled = true;
                retryUploadBtn.disabled = true;
                retryUploadBtn.textContent = "Retrying…";
                await submitAnswerFlow(pendingUpload);
                retryUploadBtn.disabled = false;
                retryUploadBtn.textContent = "Retry Upload";
            }
        });
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Quit interview Modal
    // ─────────────────────────────────────────────────────────────────────────

    if (quitBtn && quitModal) {
        quitBtn.addEventListener("click", () => {
            quitModal.style.display = "flex";
        });
    }

    if (quitConfirmClose) {
        quitConfirmClose.addEventListener("click", () => {
            if (quitModal) quitModal.style.display = "none";
        });
    }

    if (quitConfirmOk) {
        quitConfirmOk.addEventListener("click", async () => {
            if (quitModal) quitModal.style.display = "none";
            interviewEnded = true;
            stopSpeech();
            stopTimer();

            if (window.cameraManager && window.cameraManager.isRecording) {
                await window.cameraManager.stopRecording().catch(() => {});
            }
            if (window.cameraManager) {
                window.cameraManager.cleanup();
            }

            try {
                await API.abandonSession(window.state.sessionId);
            } catch (e) {
                console.warn("Could not mark session as abandoned:", e);
            }

            window.location.href = "/dashboard";
        });
    }

    if (quitModal) {
        quitModal.addEventListener("click", (e) => {
            if (e.target === quitModal) quitModal.style.display = "none";
        });
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Page Unload Cleanup
    // ─────────────────────────────────────────────────────────────────────────

    window.addEventListener("beforeunload", () => {
        stopSpeech();
        stopTimer();
        if (window.cameraManager && !interviewEnded) {
            window.cameraManager.cleanup();
        }
    });

    window.addEventListener("pagehide", () => {
        stopSpeech();
    });
});
