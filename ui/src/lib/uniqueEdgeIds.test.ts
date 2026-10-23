import { expect, it } from 'vitest';

import { uniqueEdgeIds } from './uniqueEdgeIds';

it('retains every parallel branch and avoids collisions with existing suffixed IDs', () => {
    const edges = [
        { id: '3-35', data: { condition: 'first' } },
        { id: '3-35', data: { condition: 'second' } },
        { id: '3-35__2', data: { condition: 'third' } },
        { id: '3-35', data: { condition: 'fourth' } },
    ];
    const normalized = uniqueEdgeIds(edges);
    expect(normalized.map(edge => edge.id)).toEqual(['3-35', '3-35__3', '3-35__2', '3-35__4']);
    expect(normalized.map(edge => edge.data)).toEqual(edges.map(edge => edge.data));
    expect(edges[1].id).toBe('3-35');
    expect(uniqueEdgeIds(normalized)).toBe(normalized);
});
