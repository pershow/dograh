"""Apply explicit knowledge bindings and retain the evidence actually returned."""

import copy


def applicable_documents(document_ids, state):
    if not state.contract.enabled:
        return list(document_ids)
    selected = []
    for identity in document_ids:
        binding = state.contract.knowledge_bindings.get(identity)
        if binding is None or all(
            type(state.data["facts"].get(key, {}).get("value")) is type(expected)
            and state.data["facts"].get(key, {}).get("value") == expected
            for key, expected in binding.applies_when.items()
        ):
            selected.append(identity)
    return selected


def bind_knowledge_evidence(result, state, *, allowed_documents=None):
    result = copy.deepcopy(result)
    accepted, excluded = [], []
    for chunk in result.get("chunks", []):
        if (
            allowed_documents is not None
            and chunk.get("document_uuid")
            and chunk["document_uuid"] not in allowed_documents
        ):
            excluded.append(
                {
                    "reason": "source_not_applicable",
                    "document_uuid": chunk["document_uuid"],
                }
            )
            continue
        if (
            state.contract.enabled
            and state.contract.knowledge_bindings
            and not chunk.get("document_uuid")
        ):
            excluded.append({"reason": "missing_source_identity"})
            continue
        binding = (
            state.contract.knowledge_bindings.get(chunk.get("document_uuid"))
            if state.contract.enabled
            else None
        )
        if (
            binding
            and binding.version
            and binding.version != chunk.get("document_version")
        ):
            excluded.append(
                {
                    "document_uuid": chunk.get("document_uuid"),
                    "reason": "version_mismatch",
                }
            )
            continue
        chunk["purpose"] = binding.purpose if binding else "knowledge"
        accepted.append(chunk)
    result.update(chunks=accepted, total_results=len(accepted), excluded=excluded)
    if not accepted:
        result.update(
            status="insufficient_evidence",
            instruction="Clarify the missing applicability facts or request verified knowledge. Do not infer a policy from an example.",
        )
    return result
