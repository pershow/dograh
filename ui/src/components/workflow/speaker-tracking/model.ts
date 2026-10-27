const WINDOW_ACTIONS = new Set([
    'candidate', 'primary_established', 'primary_match', 'primary_changed',
    'other_speaker', 'uncertain', 'skipped',
]);

export interface SpeakerDecision {
    id: number;
    action: string;
    start: number | null;
    end: number | null;
    speaker: string | null;
    primary: string | null;
    primaryReported: boolean;
    previousPrimary: string | null;
    similarity: number | null;
    candidateSimilarity: number | null;
    previousSimilarity: number | null;
    reason: string | null;
    learningAllowed: boolean | null;
    speechSeconds: number | null;
    sampleRate: number | null;
    embeddingMs: number | null;
    lagMs: number | null;
}

function object(value: unknown): Record<string, unknown> | null {
    return value !== null && typeof value === 'object' && !Array.isArray(value)
        ? value as Record<string, unknown> : null;
}

function text(value: unknown): string | null {
    return typeof value === 'string' && value.length > 0 ? value : null;
}

function number(value: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): number | null {
    return typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max ? value : null;
}

export function speakerName(value: string | null): string {
    if (!value) return 'Unassigned';
    const match = /^speaker_(\d+)$/.exec(value);
    return match ? `Speaker ${match[1]}` : value;
}

export function audioTime(seconds: number | null): string {
    if (seconds === null) return 'Time unavailable';
    const millis = Math.round(seconds * 1000);
    const minutes = Math.floor(millis / 60000);
    return `${String(minutes).padStart(2, '0')}:${String(Math.floor(millis / 1000) % 60).padStart(2, '0')}.${String(millis % 1000).padStart(3, '0')}`;
}

export function readSpeakerAnalysis(raw: unknown) {
    const decisions: SpeakerDecision[] = [];
    const counts: Record<string, number> = {};
    const observed: Record<string, number> = {};
    let eventCount = 0;
    let hasSummary = false;
    let ready = false;
    let disabledReason: string | null = null;
    let discarded = 0;

    for (const [index, value] of (Array.isArray(raw) ? raw : []).entries()) {
        const event = object(value);
        if (event?.type !== 'rtf-speaker-tracking') continue;
        eventCount++;
        const payload = object(event.payload);
        if (!payload) continue;
        const action = text(payload.action);
        if (!action) continue;
        if (action === 'ready') ready = true;
        if (action === 'disabled') disabledReason = text(payload.reason) ?? 'Unknown error';
        if (action === 'summary') {
            hasSummary = true;
            discarded = number(payload.discarded_on_finish) ?? 0;
            for (const [key, value] of Object.entries(object(payload.counts) ?? {})) {
                const count = number(value);
                if (count !== null && Number.isInteger(count)) counts[key] = count;
            }
        }
        if (!WINDOW_ACTIONS.has(action)) continue;
        observed[action] = (observed[action] ?? 0) + 1;
        const end = number(payload.audio_end_seconds);
        const start = number(payload.audio_start_seconds);
        decisions.push({
            id: index, action,
            start: start !== null && end !== null && start > end ? null : start,
            end,
            speaker: text(payload.speaker),
            primary: text(payload.primary_speaker),
            primaryReported: payload.primary_speaker === null || typeof payload.primary_speaker === 'string',
            previousPrimary: text(payload.previous_primary),
            similarity: number(payload.similarity, -1, 1),
            candidateSimilarity: number(payload.candidate_similarity, -1, 1),
            previousSimilarity: number(payload.previous_best_similarity, -1, 1),
            reason: text(payload.reason),
            learningAllowed: typeof payload.learning_allowed === 'boolean' ? payload.learning_allowed : null,
            speechSeconds: number(payload.speech_seconds),
            sampleRate: number(payload.sample_rate),
            embeddingMs: number(payload.embedding_ms),
            lagMs: number(payload.analysis_lag_ms),
        });
    }

    // Audio offsets order decisions even if persisted log delivery was delayed.
    // Legacy decisions without offsets retain their relative order at the end.
    decisions.sort((a, b) => (a.end ?? Infinity) - (b.end ?? Infinity) || a.id - b.id);
    let lastPrimary: string | null = null;
    for (const decision of decisions) {
        if (decision.action === 'primary_changed' && decision.previousPrimary === null) decision.previousPrimary = lastPrimary;
        // Skipped windows omit primary_speaker; they do not clear the reference.
        if (decision.primaryReported) lastPrimary = decision.primary;
        else decision.primary = lastPrimary;
    }
    const changes = decisions.filter((entry) => entry.action === 'primary_changed');
    const transitions = decisions.filter((entry) => entry.action === 'primary_established' || entry.action === 'primary_changed');
    const omitted = [...WINDOW_ACTIONS].reduce((sum, action) => sum + Math.max(0, (counts[action] ?? 0) - (observed[action] ?? 0)), 0);

    const diagnosticLabels: Record<string, string> = {
        queue_overflow: 'Input samples skipped: queue full',
        executor_busy: 'Analysis batches skipped: capacity reached',
        stale_audio: 'Input samples skipped: analysis fell behind',
        unsupported_audio: 'Input samples skipped: unsupported audio',
        answer_supervisor_pending: 'Input samples skipped during answer screening',
        log_error: 'Log write failures',
        finish_timeout: 'Analysis finalization timed out',
        worker_cancelled: 'Analysis worker cancelled',
    };
    const diagnostics = Object.entries(diagnosticLabels)
        .filter(([key]) => (counts[key] ?? 0) > 0)
        .map(([key, label]) => ({ key, label, count: counts[key] }));
    if (discarded > 0) diagnostics.push({ key: 'discarded', label: 'Input samples discarded at call end', count: discarded });

    return {
        eventCount, decisions, lastPrimary, transitions, changes, hasSummary, ready,
        disabledReason, diagnostics, omitted,
        changeCount: Math.max(counts.primary_changed ?? 0, changes.length),
        uncertainCount: Math.max(counts.uncertain ?? 0, observed.uncertain ?? 0),
    };
}

export const decisionLabels: Record<string, string> = {
    candidate: 'Candidate', primary_established: 'Primary established',
    primary_match: 'Primary match', primary_changed: 'Speaker changed',
    other_speaker: 'Other speaker', uncertain: 'Uncertain', skipped: 'Skipped',
};

export const reasonLabels: Record<string, string> = {
    ambiguous_similarity: 'Voice similarity is inconclusive',
    novel_voice_pending: 'Waiting for consistent evidence of a new voice',
    consistent_novel_voice: 'New voice confirmed across multiple windows',
    learning_suspended: 'Reference learning paused while the agent speaks',
    insufficient_quality: 'Too little clear speech for a reliable comparison',
    invalid_embedding: 'Voice comparison unavailable',
    speaker_capacity: 'Call speaker limit reached',
};
