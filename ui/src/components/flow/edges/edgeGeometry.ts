import { type Edge, getSmoothStepPath, Position } from '@xyflow/react';

// Keep collapsed labels on one line so adjacent lanes have predictable bounds.
export const EDGE_LABEL_MAX_WIDTH = 220;
const LABEL_GAP = 24;
const LANE_HEIGHT = 48;

export interface EdgeLane {
    index: number;
    count: number;
}

export function getParallelEdgeLane(
    edges: Pick<Edge, 'id' | 'source' | 'target' | 'hidden'>[],
    id: string,
    source: string,
    target: string,
): EdgeLane {
    const parallel = edges.filter(edge => !edge.hidden && (
        (edge.source === source && edge.target === target) ||
        (edge.source === target && edge.target === source)
    )).sort((a, b) => a.id.localeCompare(b.id));
    return {
        index: Math.max(0, parallel.findIndex(edge => edge.id === id)),
        count: Math.max(1, parallel.length),
    };
}

interface EdgeGeometryOptions {
    sourceX: number;
    sourceY: number;
    targetX: number;
    targetY: number;
    sourcePosition: Position;
    targetPosition: Position;
    lane: EdgeLane;
    isSelfLoop: boolean;
    sourceRight?: number;
}

export function getEdgeGeometry(options: EdgeGeometryOptions) {
    const { sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, lane, isSelfLoop, sourceRight } = options;
    if (lane.count === 1 && !isSelfLoop) {
        const [path, labelX, labelY] = getSmoothStepPath({
            sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
            borderRadius: 8, offset: 20,
        });
        return { path, labelX, labelY };
    }

    const dx = targetX - sourceX;
    const dy = targetY - sourceY;
    const vertical = isSelfLoop || Math.abs(dy) > Math.abs(dx);
    let labelX = (sourceX + targetX) / 2;
    let labelY = (sourceY + targetY) / 2;
    const laneOffset = lane.index - (lane.count - 1) / 2;
    if (isSelfLoop) {
        // Place the entire label outside the node, including when it is resized.
        labelX = Math.max(sourceRight ?? sourceX, sourceX, targetX)
            + EDGE_LABEL_MAX_WIDTH / 2 + 40 + lane.index * (EDGE_LABEL_MAX_WIDTH + LABEL_GAP);
    } else if (vertical) {
        labelX += laneOffset * (EDGE_LABEL_MAX_WIDTH + LABEL_GAP);
    } else {
        labelY += laneOffset * LANE_HEIGHT;
    }

    // Two routes meet at the label itself. Moving a label therefore also moves
    // its path, even for aligned nodes where smooth-step centers can be ignored.
    // Lanes use screen axes (not edge direction), so reverse edges stay distinct.
    const [labelEntry, labelExit] = vertical
        ? dy > 0 ? [Position.Top, Position.Bottom] : [Position.Bottom, Position.Top]
        : dx > 0 ? [Position.Left, Position.Right] : [Position.Right, Position.Left];
    const [beforeLabel] = getSmoothStepPath({
        sourceX, sourceY, sourcePosition,
        targetX: labelX, targetY: labelY, targetPosition: labelEntry,
        borderRadius: 8, offset: 20,
    });
    const [afterLabel] = getSmoothStepPath({
        sourceX: labelX, sourceY: labelY, sourcePosition: labelExit,
        targetX, targetY, targetPosition,
        borderRadius: 8, offset: 20,
    });
    return { path: `${beforeLabel} ${afterLabel}`, labelX, labelY };
}
