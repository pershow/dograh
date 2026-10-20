// Match api/utils/template_renderer.py and workflow_graph.extract_template_variables.
const VARIABLE_PATTERN = /\{\{\s*([^|\s}]+)(?:\s*\|\s*([^:}]+)(?::([^}]+))?)?\s*\}\}/g;
const SYSTEM_VARIABLES = new Set(["campaign_id", "provider", "source_uuid"]);

export function referencedVariables(nodes: { data?: unknown }[], edges: { data?: unknown }[] = []): string[] {
    const names = new Set<string>();
    const walk = (value: unknown) => {
        if (typeof value === "string") {
            for (const match of value.matchAll(VARIABLE_PATTERN)) {
                const name = match[1];
                if (match[2] || name.includes(".") || SYSTEM_VARIABLES.has(name) ||
                    name === "current_time" || name === "current_weekday" ||
                    name.startsWith("current_time_") || name.startsWith("current_weekday_")) continue;
                names.add(name);
            }
        } else if (value && typeof value === "object") {
            Object.values(value).forEach(walk);
        }
    };
    [...nodes, ...edges].forEach(item => walk(item.data));
    return [...names].sort();
}
