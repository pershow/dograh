import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SpeakerTrackingPanel } from './SpeakerTrackingPanel';

const event = (payload: Record<string, unknown>) => ({ type: 'rtf-speaker-tracking', payload });

describe('call speaker analysis panel', () => {
    it('shows main speaker, exact change time, scores and uncertain windows with filters', () => {
        render(<SpeakerTrackingPanel events={[
            event({ action: 'primary_established', audio_start_seconds: 3.2, audio_end_seconds: 4.864, speaker: 'speaker_1', primary_speaker: 'speaker_1', similarity: 0.6498 }),
            event({ action: 'uncertain', audio_start_seconds: 11.008, audio_end_seconds: 12.608, primary_speaker: 'speaker_1', similarity: 0, candidate_similarity: 0.79, reason: 'novel_voice_pending', learning_allowed: true }),
            event({ action: 'primary_changed', audio_start_seconds: 15.968, audio_end_seconds: 17.568, speaker: 'speaker_2', primary_speaker: 'speaker_2', previous_primary: 'speaker_1', similarity: 0.7889 }),
            event({ action: 'summary', counts: { uncertain: 1, primary_changed: 1 } }),
        ]} />);
        expect(screen.getByText('Last recorded primary').nextElementSibling?.textContent).toBe('Speaker 2');
        expect(screen.getByText('Speaker changes').nextElementSibling?.textContent).toBe('1');
        const history = within(screen.getByRole('list', { name: 'Primary speaker history' }));
        expect(history.getByText('00:17.568')).toBeTruthy();
        expect(screen.getByText('0.650')).toBeTruthy();
        expect(screen.getByText('0.000')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Uncertain only' }));
        const table = within(screen.getByRole('table', { name: 'Speaker decision windows' }));
        expect(table.getAllByRole('row')).toHaveLength(2);
        expect(table.getByText('Waiting for consistent evidence of a new voice')).toBeTruthy();
        expect(table.getByRole('cell', { name: /00:11.008.*00:12.608/ })).toBeTruthy();
        expect(table.queryByText('0.789')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Speaker changes only' }));
        expect(table.getByText('Speaker 1 → Speaker 2')).toBeTruthy();
        expect(table.getByText('0.789')).toBeTruthy();
        expect(screen.getByText(/not an identity confidence percentage/)).toBeTruthy();
    });

    it('shows missing data and stopped analysis without calling it a successful analysis', () => {
        const view = render(<SpeakerTrackingPanel events={undefined} />);
        expect(screen.getByText(/No speaker analysis was recorded/)).toBeTruthy();
        view.rerender(<SpeakerTrackingPanel events={[event({ action: 'disabled', reason: 'FileNotFoundError' })]} />);
        expect(screen.getByRole('status').textContent).toContain('FileNotFoundError');
        expect(screen.getByText(/No final summary was saved/)).toBeTruthy();
        expect(screen.queryByRole('table')).toBeNull();
    });

    it('shows overload and log truncation alongside total counts', () => {
        render(<SpeakerTrackingPanel events={[
            event({ action: 'uncertain' }),
            event({ action: 'summary', counts: { uncertain: 6, queue_overflow: 12, finish_timeout: 1 }, discarded_on_finish: 3 }),
        ]} />);
        expect(screen.getByText(/5 decision windows were not saved/)).toBeTruthy();
        expect(screen.getByText('Uncertain windows').nextElementSibling?.textContent).toBe('6');
        expect(screen.getByText('Input samples skipped: queue full: 12')).toBeTruthy();
        expect(screen.getByText('Input samples discarded at call end: 3')).toBeTruthy();
        expect(screen.getByText('Time unavailable')).toBeTruthy();
        expect(screen.queryByText('0.000')).toBeNull();
    });

    it('paginates long calls and resets pagination when filtering', () => {
        render(<SpeakerTrackingPanel events={[
            ...Array.from({ length: 45 }, (_, index) => event({ action: index === 0 ? 'uncertain' : 'primary_match', audio_start_seconds: index, audio_end_seconds: index + 1 })),
            event({ action: 'summary', counts: { uncertain: 1, primary_match: 44 } }),
        ]} />);
        expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(21);
        fireEvent.click(screen.getByRole('button', { name: 'Next speaker analysis page' }));
        expect(screen.getByText('21–40 of 45 saved windows')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Uncertain only' }));
        expect(screen.getByText('1–1 of 1 saved windows')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Speaker changes only' }));
        expect(screen.getByText('No saved windows match this filter.')).toBeTruthy();
    });
});
