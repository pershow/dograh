import { describe, expect, it } from 'vitest';

import { audioTime, readSpeakerAnalysis } from './model';

const event = (payload: Record<string, unknown>) => ({ type: 'rtf-speaker-tracking', mode: 'shadow', version: 1, payload });

describe('saved speaker analysis', () => {
    it('orders audio windows and preserves the primary across skipped windows', () => {
        const analysis = readSpeakerAnalysis([
            event({ action: 'primary_changed', audio_start_seconds: 8, audio_end_seconds: 9.6, speaker: 'speaker_2', primary_speaker: 'speaker_2', previous_primary: 'speaker_1', similarity: 0.73 }),
            event({ action: 'primary_established', audio_start_seconds: 3.2, audio_end_seconds: 4.8, speaker: 'speaker_1', primary_speaker: 'speaker_1', similarity: 0.65 }),
            event({ action: 'skipped', audio_start_seconds: 10, audio_end_seconds: 10.5 }),
        ]);
        expect(analysis.decisions.map((row) => row.end)).toEqual([4.8, 9.6, 10.5]);
        expect(analysis.lastPrimary).toBe('speaker_2');
        expect(analysis.decisions[2].primary).toBe('speaker_2');
        expect(analysis.transitions).toHaveLength(2);
        expect(analysis.changeCount).toBe(1);
    });

    it('never promotes a candidate to primary, treats zero as a real score and leaves missing values empty', () => {
        const analysis = readSpeakerAnalysis([
            event({ action: 'candidate', speaker: 'speaker_1', similarity: null }),
            event({ action: 'uncertain', similarity: 0 }),
            event({ action: 'other_speaker', similarity: -0.25 }),
        ]);
        expect(analysis.lastPrimary).toBeNull();
        expect(analysis.decisions.map((row) => row.similarity)).toEqual([null, 0, -0.25]);
        expect(analysis.decisions.every((row) => row.end === null)).toBe(true);
    });

    it('exposes missing saved windows and diagnostics without inventing their speaker or times', () => {
        const analysis = readSpeakerAnalysis([
            event({ action: 'uncertain', audio_start_seconds: 5, audio_end_seconds: 6 }),
            event({ action: 'summary', counts: { uncertain: 7, primary_changed: 2, queue_overflow: 4, executor_busy: 3 }, discarded_on_finish: 5 }),
        ]);
        expect(analysis.uncertainCount).toBe(7);
        expect(analysis.changeCount).toBe(2);
        expect(analysis.omitted).toBe(8);
        expect(analysis.changes).toEqual([]);
        expect(analysis.diagnostics.map((row) => row.count)).toEqual([4, 3, 5]);
    });

    it('tolerates historical or malformed records and ignores unrelated events', () => {
        expect(readSpeakerAnalysis(null).eventCount).toBe(0);
        expect(readSpeakerAnalysis({}).decisions).toEqual([]);
        const analysis = readSpeakerAnalysis([
            null, [], { type: 'rtf-user-transcription', payload: { text: 'hello' } },
            event({ action: 'uncertain', similarity: '0.9', candidate_similarity: NaN, audio_start_seconds: 10, audio_end_seconds: 2 }),
            event({ action: 'candidate', similarity: Infinity, audio_end_seconds: -2, speaker: {} }),
            event({ action: 'disabled', reason: 'FileNotFoundError' }),
            event({ action: 'summary', counts: { uncertain: '3', queue_overflow: -5 } }),
        ]);
        expect(analysis.disabledReason).toBe('FileNotFoundError');
        expect(analysis.decisions[0].start).toBeNull();
        expect(analysis.decisions.every((row) => row.similarity === null)).toBe(true);
        expect(analysis.uncertainCount).toBe(1);
        expect(analysis.diagnostics).toEqual([]);
    });

    it('formats millisecond offsets and carries rounding into the next minute', () => {
        expect(audioTime(0)).toBe('00:00.000');
        expect(audioTime(59.9996)).toBe('01:00.000');
        expect(audioTime(125.12)).toBe('02:05.120');
        expect(audioTime(null)).toBe('Time unavailable');
    });
});
