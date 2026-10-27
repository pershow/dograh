import { getSmoothStepPath, Position } from '@xyflow/react';
import { describe, expect, it } from 'vitest';

import { EDGE_LABEL_MAX_WIDTH, getEdgeGeometry, getParallelEdgeLane } from './edgeGeometry';

const diagonal = {
    sourceX: 950, sourceY: 700, targetX: 350, targetY: 900,
    sourcePosition: Position.Bottom, targetPosition: Position.Top,
    isSelfLoop: false,
};

describe('parallel edge geometry', () => {
    it('preserves the existing single-edge route', () => {
        const result = getEdgeGeometry({ ...diagonal, lane: { index: 0, count: 1 } });
        const [path, labelX, labelY] = getSmoothStepPath({ ...diagonal, borderRadius: 8, offset: 20 });
        expect(result).toEqual({ path, labelX, labelY });
    });

    for (const [name, geometry] of Object.entries({
        diagonal,
        vertical: { ...diagonal, targetX: 950 },
        horizontal: { ...diagonal, targetY: 700, sourcePosition: Position.Left, targetPosition: Position.Right },
        reverse: { ...diagonal, sourceX: 350, sourceY: 900, targetX: 950, targetY: 700 },
        selfLoop: { ...diagonal, targetX: 950, targetY: 500, isSelfLoop: true, sourceRight: 1400 },
    })) {
        it.each([2, 3, 7, 12])(`keeps all %i ${name} branches separate with their labels on their paths`, count => {
            const routes = Array.from({ length: count }, (_, index) => getEdgeGeometry({
                ...geometry, lane: { index, count },
            }));
            expect(new Set(routes.map(route => route.path)).size).toBe(count);
            for (const route of routes) {
                // Every route starts/ends at the actual handles and passes through
                // its label. This catches label-only offsets and detached labels.
                const coordinates = route.path.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
                expect(coordinates.slice(0, 2)).toEqual([geometry.sourceX, geometry.sourceY]);
                expect(coordinates.slice(-2)).toEqual([geometry.targetX, geometry.targetY]);
                const [beforeLabel, afterLabel] = route.path.split(' M');
                const first = beforeLabel.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
                const second = afterLabel.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
                expect(first.slice(-2)).toEqual([route.labelX, route.labelY]);
                expect(second.slice(0, 2)).toEqual([route.labelX, route.labelY]);
                expect(coordinates.every(Number.isFinite)).toBe(true);
                if ('sourceRight' in geometry) {
                    expect(route.labelX - EDGE_LABEL_MAX_WIDTH / 2).toBeGreaterThan(geometry.sourceRight);
                }
            }
            for (let i = 0; i < routes.length; i++) {
                for (const other of routes.slice(i + 1)) {
                    const separateX = Math.abs(routes[i].labelX - other.labelX) > EDGE_LABEL_MAX_WIDTH;
                    const separateY = Math.abs(routes[i].labelY - other.labelY) > 28;
                    expect(separateX || separateY).toBe(true);
                }
            }
        });
    }

    it('assigns stable lanes across input order, reverse directions and hidden/unrelated edges', () => {
        const edges = [
            { id: 'c', source: 'a', target: 'b' },
            { id: 'b', source: 'b', target: 'a' },
            { id: 'a', source: 'a', target: 'b' },
            { id: 'hidden', source: 'a', target: 'b', hidden: true },
            { id: 'unrelated', source: 'a', target: 'other' },
        ];
        const before = structuredClone(edges);
        for (const [index, id] of ['a', 'b', 'c'].entries()) {
            expect(getParallelEdgeLane(edges, id, 'a', 'b')).toEqual({ index, count: 3 });
            expect(getParallelEdgeLane([...edges].reverse(), id, 'b', 'a')).toEqual({ index, count: 3 });
        }
        expect(edges).toEqual(before);
        const forward = getEdgeGeometry({ ...diagonal, lane: { index: 0, count: 2 } });
        const backward = getEdgeGeometry({
            ...diagonal, sourceX: diagonal.targetX, sourceY: diagonal.targetY,
            targetX: diagonal.sourceX, targetY: diagonal.sourceY, lane: { index: 1, count: 2 },
        });
        expect(forward.labelY).not.toBe(backward.labelY);
    });
});
