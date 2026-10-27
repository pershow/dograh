'use client';

import { ArrowRight, UsersRound } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { cn } from '@/lib/utils';

import { audioTime, decisionLabels, readSpeakerAnalysis, reasonLabels, type SpeakerDecision, speakerName } from './model';

const PAGE_SIZE = 20;
type Filter = 'all' | 'uncertain' | 'changes';

function SummaryMetric({ label, value }: { label: string; value: string | number }) {
    return <div className="rounded-lg border bg-muted/30 p-3">
        <dt className="text-xs text-muted-foreground">{label}</dt>
        <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
    </div>;
}

function DecisionRow({ decision }: { decision: SpeakerDecision }) {
    const reason = decision.reason ? reasonLabels[decision.reason] ?? decision.reason : null;
    const isChange = decision.action === 'primary_changed';
    const hasMeasurements = [decision.candidateSimilarity, decision.previousSimilarity, decision.speechSeconds, decision.sampleRate, decision.embeddingMs, decision.lagMs].some((value) => value !== null);
    return <TableRow className={cn(
        decision.action === 'uncertain' && 'bg-amber-50/60 dark:bg-amber-950/20',
        isChange && 'bg-sky-50/60 dark:bg-sky-950/20',
    )}>
        <TableCell className="whitespace-nowrap align-top font-mono text-xs tabular-nums">
            {decision.start !== null && decision.end !== null
                ? <>{audioTime(decision.start)}<br />– {audioTime(decision.end)}</>
                : 'Time unavailable'}
        </TableCell>
        <TableCell className="align-top">
            <div className="whitespace-nowrap font-medium">{speakerName(decision.speaker)}</div>
            <div className="mt-1 whitespace-nowrap text-xs text-muted-foreground">Primary: {speakerName(decision.primary)}</div>
        </TableCell>
        <TableCell className="align-top">
            <Badge variant="outline" className={cn('whitespace-nowrap',
                decision.action === 'uncertain' && 'border-amber-300 text-amber-800 dark:border-amber-700 dark:text-amber-300',
                isChange && 'border-sky-300 text-sky-800 dark:border-sky-700 dark:text-sky-300',
            )}>{decisionLabels[decision.action]}</Badge>
            {isChange && <p className="mt-1 text-xs text-muted-foreground">
                {speakerName(decision.previousPrimary)} → {speakerName(decision.primary)}
            </p>}
        </TableCell>
        <TableCell className="align-top font-mono tabular-nums">
            {decision.similarity === null ? <span title="No comparison score recorded">—</span> : decision.similarity.toFixed(3)}
        </TableCell>
        <TableCell className="min-w-48 max-w-72 align-top text-xs">
            {reason && <p>{reason}</p>}
            {decision.learningAllowed === false && decision.reason !== 'learning_suspended' && <p>Reference learning paused while the agent speaks.</p>}
            {hasMeasurements && <details className="mt-1 text-muted-foreground">
                <summary className="cursor-pointer">Measurement details</summary>
                <dl className="mt-2 space-y-1">
                    {decision.candidateSimilarity !== null && <div><dt className="inline">New candidate similarity: </dt><dd className="inline tabular-nums">{decision.candidateSimilarity.toFixed(3)}</dd></div>}
                    {decision.previousSimilarity !== null && <div><dt className="inline">Closest previous reference: </dt><dd className="inline tabular-nums">{decision.previousSimilarity.toFixed(3)}</dd></div>}
                    {decision.speechSeconds !== null && <div><dt className="inline">Speech: </dt><dd className="inline">{decision.speechSeconds.toFixed(2)} s</dd></div>}
                    {decision.sampleRate !== null && <div><dt className="inline">Input: </dt><dd className="inline">{decision.sampleRate / 1000} kHz</dd></div>}
                    {decision.embeddingMs !== null && <div><dt className="inline">Voice extraction: </dt><dd className="inline">{decision.embeddingMs.toFixed(1)} ms</dd></div>}
                    {decision.lagMs !== null && <div><dt className="inline">Analysis lag: </dt><dd className="inline">{decision.lagMs.toFixed(1)} ms</dd></div>}
                </dl>
            </details>}
        </TableCell>
    </TableRow>;
}

export function SpeakerTrackingPanel({ events }: { events: unknown }) {
    const analysis = useMemo(() => readSpeakerAnalysis(events), [events]);
    const [filter, setFilter] = useState<Filter>('all');
    const [page, setPage] = useState(0);
    const filtered = analysis.decisions.filter((decision) => filter === 'all'
        || (filter === 'uncertain' && decision.action === 'uncertain')
        || (filter === 'changes' && decision.action === 'primary_changed'));
    const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    const currentPage = Math.min(page, pageCount - 1);
    const rows = filtered.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);

    return <Card className="min-w-0 border-border" aria-label="Speaker analysis">
        <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="flex items-center gap-2 text-lg"><UsersRound className="h-5 w-5" />Speaker Analysis</CardTitle>
                <Badge variant="secondary">Shadow mode</Badge>
            </div>
            <CardDescription>Call-local voice hypotheses for review. These decisions did not filter speech or change interruptions.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
            {analysis.eventCount === 0 ? <p className="text-sm text-muted-foreground">
                No speaker analysis was recorded for this call. Enable Speaker Tracking (Preview) in the agent’s General settings, save and publish before making a new call.
            </p> : <>
                {analysis.disabledReason && <div role="status" className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-200">
                    Speaker analysis stopped: {analysis.disabledReason}. The call continued with its usual transcription and interruption behavior.
                </div>}
                {(!analysis.hasSummary || analysis.omitted > 0 || analysis.diagnostics.length > 0) && <div className="rounded-lg border bg-muted/30 p-3 text-xs" role="note">
                    <p className="font-medium">Analysis coverage</p>
                    {!analysis.hasSummary && <p className="mt-1">No final summary was saved. These results may be incomplete.</p>}
                    {analysis.omitted > 0 && <p className="mt-1">{analysis.omitted} decision windows were not saved. Totals include them; speaker details below cover saved windows only.</p>}
                    {analysis.diagnostics.length > 0 && <ul className="mt-1 space-y-1">
                        {analysis.diagnostics.map((item) => <li key={item.key}>{item.label}: {item.count}</li>)}
                    </ul>}
                </div>}
                <dl className="grid grid-cols-2 gap-3 xl:grid-cols-4">
                    <SummaryMetric label="Last recorded primary" value={speakerName(analysis.lastPrimary)} />
                    <SummaryMetric label="Speaker changes" value={analysis.changeCount} />
                    <SummaryMetric label="Uncertain windows" value={analysis.uncertainCount} />
                    <SummaryMetric label="Saved windows" value={analysis.decisions.length} />
                </dl>
                <p className="text-xs text-muted-foreground">
                    Times are offsets in received caller audio and may differ from recording playback.
                    A change time marks when it was confirmed, not the new speaker’s first word.
                    Similarity is a cosine score from −1 to 1 against a voice reference, not an identity confidence percentage.
                    Speaker labels apply only to this call.
                </p>
                {analysis.transitions.length > 0 && <details open className="rounded-lg border p-3">
                    <summary className="cursor-pointer text-sm font-medium">Primary speaker history</summary>
                    <ol className="mt-3 max-h-48 space-y-2 overflow-y-auto pr-2" aria-label="Primary speaker history">
                        {analysis.transitions.map((entry) => <li key={entry.id} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
                            <span className="font-mono text-xs tabular-nums">{audioTime(entry.end)}</span>
                            {entry.action === 'primary_changed' ? <><span>{speakerName(entry.previousPrimary)}</span><ArrowRight aria-label="changed to" className="h-3.5 w-3.5 text-muted-foreground" /></> : <span className="text-muted-foreground">Primary established:</span>}
                            <span className="font-medium">{speakerName(entry.primary)}</span>
                        </li>)}
                    </ol>
                </details>}
                {analysis.decisions.length === 0 ? <p className="text-sm text-muted-foreground">
                    {analysis.disabledReason ? 'No voice comparison windows were saved.' : analysis.ready
                        ? 'The model was ready, but no eligible speech windows were saved.'
                        : 'No voice comparison windows were saved for this call.'}
                </p> : <>
                    <div className="flex flex-wrap gap-2" role="group" aria-label="Filter speaker decisions">
                        {(['all', 'uncertain', 'changes'] as const).map((value) => <Button
                            key={value} size="sm" variant={filter === value ? 'secondary' : 'outline'}
                            aria-pressed={filter === value}
                            onClick={() => { setFilter(value); setPage(0); }}
                        >{value === 'all' ? 'All windows' : value === 'uncertain' ? 'Uncertain only' : 'Speaker changes only'}</Button>)}
                    </div>
                    <Table aria-label="Speaker decision windows">
                        <TableHeader><TableRow>
                            <TableHead>Audio window</TableHead><TableHead>Speaker</TableHead><TableHead>Decision</TableHead><TableHead>Similarity</TableHead><TableHead>Notes</TableHead>
                        </TableRow></TableHeader>
                        <TableBody>{rows.length > 0 ? rows.map((decision) => <DecisionRow key={decision.id} decision={decision} />)
                            : <TableRow><TableCell colSpan={5} className="py-6 text-center text-muted-foreground">No saved windows match this filter.</TableCell></TableRow>}
                        </TableBody>
                    </Table>
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                        <span aria-live="polite">{filtered.length === 0 ? '0 windows' : `${currentPage * PAGE_SIZE + 1}–${Math.min((currentPage + 1) * PAGE_SIZE, filtered.length)} of ${filtered.length} saved windows`}</span>
                        {pageCount > 1 && <div className="flex items-center gap-2">
                            <Button size="sm" variant="outline" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)} aria-label="Previous speaker analysis page">Previous</Button>
                            <span>Page {currentPage + 1} of {pageCount}</span>
                            <Button size="sm" variant="outline" disabled={currentPage >= pageCount - 1} onClick={() => setPage(currentPage + 1)} aria-label="Next speaker analysis page">Next</Button>
                        </div>}
                    </div>
                </>}
            </>}
        </CardContent>
    </Card>;
}
