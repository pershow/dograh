import { useCallback, useState } from "react";

import { useWorkflowOptional } from "@/app/workflow/[workflowId]/contexts/WorkflowContext";
import { useWorkflowStore } from "@/app/workflow/[workflowId]/stores/workflowStore";
import { FlowNodeData } from "@/components/flow/types";

interface UseNodeHandlersProps {
    id: string;
    additionalData?: Record<string, string | boolean>;
}

export const useNodeHandlers = ({ id, additionalData = {} }: UseNodeHandlersProps) => {
    const readOnly = useWorkflowOptional()?.readOnly ?? false;
    const [open, setOpen] = useState(false);
    const updateNode = useWorkflowStore((state) => state.updateNode);
    const deleteNode = useWorkflowStore((state) => state.deleteNode);
    const nodes = useWorkflowStore((state) => state.nodes);

    const handleSaveNodeData = useCallback(
        (updatedData: FlowNodeData) => {
            if (readOnly) return;
            // Find the current node to merge data properly
            const currentNode = nodes.find(node => node.id === id);
            if (currentNode) {
                updateNode(id, {
                    data: { ...currentNode.data, ...updatedData, ...additionalData }
                });
            }
        },
        [id, updateNode, additionalData, nodes, readOnly]
    );

    const handleDeleteNode = useCallback(() => {
        if (!readOnly) deleteNode(id);
    }, [id, deleteNode, readOnly]);

    return {
        open,
        setOpen,
        handleSaveNodeData,
        handleDeleteNode,
    };
};
