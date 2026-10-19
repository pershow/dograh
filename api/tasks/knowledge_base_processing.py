"""Local knowledge extraction/chunking, configured embeddings and atomic indexing."""

import asyncio
import hashlib
import mimetypes
import tempfile
from pathlib import Path

from fastapi import HTTPException
from loguru import logger

from api.db import db_client
from api.services.documents.knowledge_base import (
    MAX_FILE_SIZE_BYTES,
    prepare_embeddings,
    process_local_document,
    validate_filename,
)
from api.services.documents.parsing import FileParsingError
from api.services.storage import storage_fs

# DashScope supports at most 10 inputs per embedding request. Keep every batch bounded.
EMBEDDING_BATCH_SIZE = 10


async def _embed_texts_in_batches(
    embedding_service, texts, batch_size=EMBEDDING_BATCH_SIZE
):
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    embeddings = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        result = await embedding_service.embed_texts(batch)
        if len(result) != len(batch):
            raise FileParsingError("Embedding response count does not match input")
        embeddings.extend(result)
    return embeddings


def _safe_error(error):
    if isinstance(error, HTTPException):
        detail = error.detail
        return (
            str(detail.get("message", detail.get("code")))[:600]
            if isinstance(detail, dict)
            else str(detail)[:600]
        )
    if isinstance(error, FileParsingError):
        return str(error)[:600]
    if isinstance(error, TimeoutError):
        return "Document processing timed out. Retry or split the file into smaller documents."
    return "Document processing failed. Check the file and Embedding connection, then retry."


async def process_knowledge_base_document(
    ctx,
    document_id,
    s3_key,
    organization_id,
    created_by_provider_id,
    max_tokens=128,
    retrieval_mode="chunked",
):
    # Keep the ARQ signature compatible; user identity no longer authorizes MPS.
    document = await db_client.get_document_by_id(
        document_id, organization_id=organization_id
    )
    if not document or not s3_key.startswith(f"knowledge_base/{organization_id}/"):
        return
    token = await db_client.claim_document_processing(
        document_id, organization_id, s3_key
    )
    if not token:
        return
    file_metadata = {}
    try:
        async with asyncio.timeout(240):
            validate_filename(document.filename)
            if retrieval_mode != document.retrieval_mode:
                raise FileParsingError(
                    "Queued retrieval mode does not match the document"
                )
            service, resolved = (
                await prepare_embeddings(organization_id)
                if retrieval_mode == "chunked"
                else (None, None)
            )
            with tempfile.TemporaryDirectory(prefix="dograh-kb-") as directory:
                path = Path(directory) / (
                    "input" + Path(document.filename).suffix.lower()
                )
                if not await storage_fs.adownload_file(s3_key, str(path)):
                    raise FileParsingError(
                        "Source file could not be downloaded. Upload it again or retry."
                    )
                if not 0 < path.stat().st_size <= MAX_FILE_SIZE_BYTES:
                    raise FileParsingError("Document must be between 1 byte and 5 MiB")
                data = await asyncio.to_thread(path.read_bytes)
                file_metadata = {
                    "file_size_bytes": len(data),
                    "file_hash": hashlib.sha256(data).hexdigest(),
                    "mime_type": mimetypes.guess_type(document.filename)[0]
                    or "application/octet-stream",
                }
                duplicate = await db_client.get_document_by_hash(
                    file_metadata["file_hash"], organization_id
                )
                if duplicate and duplicate.id != document_id:
                    raise FileParsingError(
                        "Identical content already exists in this organization's knowledge base"
                    )
                result = await process_local_document(path, retrieval_mode, max_tokens)
            chunks = result["chunks"]
            if service:
                vectors = await _embed_texts_in_batches(
                    service, [c["contextualized_text"] for c in chunks]
                )
                import math

                for chunk, vector in zip(chunks, vectors, strict=True):
                    if len(vector) != 1536 or any(not math.isfinite(v) for v in vector):
                        raise FileParsingError(
                            "Embedding model must return finite 1536-dimensional vectors. Configure dimensions=1536 before retrying."
                        )
                    chunk.update(
                        embedding=vector,
                        embedding_model=service.get_model_id(),
                        embedding_dimension=1536,
                    )
                result["metadata"]["embedding_configuration"] = resolved.snapshot[
                    "services"
                ]["embeddings"]
                result["metadata"]["model_provenance"] = resolved.provenance
            await db_client.finish_document_processing(
                document_id,
                organization_id,
                token,
                full_text=result["full_text"],
                chunks=chunks,
                metadata=result["metadata"],
                file_metadata=file_metadata,
            )
    except asyncio.CancelledError:
        await asyncio.shield(
            db_client.finish_document_processing(
                document_id,
                organization_id,
                token,
                error="Processing was interrupted. Retry to resume.",
            )
        )
        raise
    except Exception as error:  # noqa: BLE001 - task boundary persists safe failure status
        # Provider errors may echo secrets/content. Persist and log only safe diagnostics.
        logger.warning(
            "Knowledge processing failed: document={}, type={}",
            document_id,
            type(error).__name__,
        )
        await db_client.finish_document_processing(
            document_id,
            organization_id,
            token,
            error=_safe_error(error),
            file_metadata=file_metadata,
        )
