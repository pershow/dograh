/** Keep every branch in legacy graphs whose parallel edges share an ID. */
export function uniqueEdgeIds<T extends { id: string }>(edges: T[]): T[] {
    const reserved = new Set(edges.map(edge => edge.id));
    const seen = new Set<string>();
    let changed = false;
    const result = edges.map(edge => {
        if (!seen.has(edge.id)) {
            seen.add(edge.id);
            return edge;
        }
        let suffix = 2;
        let id = `${edge.id}__${suffix}`;
        while (reserved.has(id)) id = `${edge.id}__${++suffix}`;
        reserved.add(id);
        changed = true;
        return { ...edge, id };
    });
    return changed ? result : edges;
}
