"""Local extraction/chunking and existing catalog bindings for knowledge files."""

from pathlib import Path

from fastapi import HTTPException

from api.db import db_client
from api.services.configuration.ai_model_configuration import (
    apply_managed_embeddings_base_url,
    get_resolved_ai_model_configuration,
)
from api.services.documents.parsing import FileParsingError, parse_local_file
from api.services.gen_ai import build_embedding_service

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
DOCUMENT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".xlsx"}
MAX_CHUNKS = 4096


def validate_filename(filename):
    if (
        not filename
        or len(filename) > 255
        or any(c in filename for c in ("/", "\\", "\x00"))
    ):
        raise HTTPException(422, detail="Use a filename without path components")
    if Path(filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise HTTPException(
            415,
            detail="Supported documents: TXT, MD, CSV, JSON, PDF, DOCX, XLSX. Export .doc files as .docx.",
        )
    return filename


async def prepare_embeddings(organization_id):
    resolved = await get_resolved_ai_model_configuration(
        organization_id=organization_id
    )
    cfg = resolved.effective.embeddings
    if not cfg or not cfg.api_key:
        raise HTTPException(
            422,
            detail={
                "code": "embedding_configuration_missing",
                "message": "Configure an Embedding role in your organization default Models configuration, or use Full Document mode.",
            },
        )
    service = await build_embedding_service(
        db_client=db_client,
        provider=cfg.provider,
        api_key=cfg.api_key,
        model=cfg.model,
        base_url=apply_managed_embeddings_base_url(
            provider=cfg.provider, base_url=getattr(cfg, "base_url", None)
        ),
        endpoint=getattr(cfg, "endpoint", None),
        api_version=getattr(cfg, "api_version", None),
        dimensions=getattr(cfg, "dimensions", None),
        resolve_correlation=True,
    )
    return service, resolved


def chunk_segments(segments, max_tokens):
    """UTF-8 byte budget is a conservative bound, not a model-specific tokenizer.

    No tokenizer downloads, Unicode replacement characters or lost source text.
    Source offsets are Python character offsets within the extracted segment.
    """
    if not 32 <= max_tokens <= 2048:
        raise FileParsingError("Chunk budget must be between 32 and 2048")
    chunks = []
    for segment in segments:
        text, start = segment["text"], 0
        while start < len(text):
            end, size = start, 0
            while (
                end < len(text) and size + len(text[end].encode("utf-8")) <= max_tokens
            ):
                size += len(text[end].encode("utf-8"))
                end += 1
            # Prefer nearby paragraph/sentence boundaries without dropping text.
            if end < len(text):
                for pos in range(end - 1, start + (end - start) // 2, -1):
                    if text[pos] in "\n。！？.!?":
                        end = pos + 1
                        break
            piece = text[start:end]
            if piece.strip():
                chunks.append(
                    {
                        "chunk_text": piece,
                        "contextualized_text": piece,
                        "chunk_index": len(chunks),
                        "token_count": len(piece.encode("utf-8")),
                        "chunk_metadata": {
                            "source_location": segment["location"],
                            "start": start,
                            "end": end,
                            "token_count_method": "utf8_byte_upper_bound",
                        },
                    }
                )
            start = end
            if len(chunks) > MAX_CHUNKS:
                raise FileParsingError(
                    "Document exceeds 4096 chunks; split it into smaller files"
                )
    return chunks


async def process_local_document(path, retrieval_mode, max_tokens=128):
    if retrieval_mode not in {"full_document", "chunked"}:
        raise FileParsingError("Unsupported retrieval mode")
    try:
        parsed = await parse_local_file(path)
    except FileParsingError as error:
        if "no extractable text" in str(error):
            raise FileParsingError(
                "No extractable text. Scanned PDFs require OCR or a text export."
            ) from error
        raise
    if parsed.get("kind") != "text" or not parsed.get("text", "").strip():
        raise FileParsingError(
            "No extractable text. Scanned PDFs require OCR or a text export."
        )
    if parsed.get("truncated"):
        raise FileParsingError(
            "Extracted text exceeds 200,000 characters; split the file before indexing"
        )
    return {
        "full_text": parsed["text"],
        "chunks": chunk_segments(parsed["segments"], max_tokens)
        if retrieval_mode == "chunked"
        else [],
        "metadata": {
            "processor": "local-v1",
            "source_locations": [s["location"] for s in parsed["segments"]],
            "chunk_budget": max_tokens if retrieval_mode == "chunked" else None,
            "chunk_budget_method": "utf8_byte_upper_bound",
            "ocr": False,
        },
    }
