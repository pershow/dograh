"""Local extraction, scoped jobs, atomic retries and configured embeddings."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter

from api.services.documents.knowledge_base import chunk_segments, process_local_document
from api.services.documents.parsing import FileParsingError
from api.tasks import knowledge_base_processing as task
from api.tests.test_agent_workbench_db import db_session as transactional_db_session
from api.tests.test_knowledge_base_content_edit import _make_user

db_session = transactional_db_session


async def test_unicode_chunks_keep_source_offsets_and_budget(tmp_path):
    text = "咨询收费为一百二十元。\n" * 15 + "A😀 final sentence."
    chunks = chunk_segments([{"location": "page 2", "text": text}], 64)
    assert "".join(c["chunk_text"] for c in chunks) == text
    assert all(c["token_count"] <= 64 for c in chunks)
    for chunk in chunks:
        meta = chunk["chunk_metadata"]
        assert meta["source_location"] == "page 2"
        assert text[meta["start"] : meta["end"]] == chunk["chunk_text"]
    path = tmp_path / "notes.md"
    path.write_text(text)
    parsed = await process_local_document(path, "full_document")
    assert parsed["full_text"] == text and parsed["chunks"] == []


async def test_scan_empty_and_truncation_are_not_success(tmp_path):
    pdf = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    writer.write(pdf)
    with pytest.raises(FileParsingError):
        await process_local_document(pdf, "full_document")
    text = tmp_path / "empty.txt"
    text.write_text("  ")
    with pytest.raises(FileParsingError):
        await process_local_document(text, "full_document")
    text.write_text("a" * 200001)
    with pytest.raises(FileParsingError, match="200,000"):
        await process_local_document(text, "chunked")


async def setup_document(db, slug, mode="full_document"):
    user = await _make_user(db, slug)
    uid = str(uuid.uuid4())
    key = f"knowledge_base/{user.selected_organization_id}/{uid}/rules.txt"
    doc = await db.create_document(
        organization_id=user.selected_organization_id,
        created_by=user.id,
        filename="rules.txt",
        file_size_bytes=0,
        file_hash="",
        mime_type="text/plain",
        custom_metadata={"s3_key": key},
        document_uuid=uid,
        retrieval_mode=mode,
    )
    return user, doc, key


async def test_full_document_job_and_retry_never_need_models(db_session, monkeypatch):
    user, doc, key = await setup_document(db_session, "local_job")
    monkeypatch.setattr(task, "db_client", db_session)
    monkeypatch.setattr(
        task,
        "prepare_embeddings",
        AsyncMock(side_effect=AssertionError("must not resolve models")),
    )
    body = b"Original policy"

    async def download(key, path):
        from pathlib import Path

        Path(path).write_bytes(body)
        return True

    monkeypatch.setattr(task.storage_fs, "adownload_file", download)
    args = (
        {},
        doc.id,
        key,
        user.selected_organization_id,
        "user",
        128,
        "full_document",
    )
    await task.process_knowledge_base_document(*args)
    stored = await db_session.get_document_by_id(
        doc.id, organization_id=user.selected_organization_id
    )
    assert (
        stored.full_text == "Original policy"
        and stored.processing_status == "completed"
    )
    assert stored.docling_metadata["processor"] == "local-v1"
    body = b"   "
    await db_session.queue_document_retry(
        doc.document_uuid, user.selected_organization_id
    )
    await task.process_knowledge_base_document(*args)
    stored = await db_session.get_document_by_id(doc.id)
    assert (
        stored.processing_status == "failed" and stored.full_text == "Original policy"
    )
    body = b"Repaired policy"
    await db_session.queue_document_retry(
        doc.document_uuid, user.selected_organization_id
    )
    await task.process_knowledge_base_document(*args)
    stored = await db_session.get_document_by_id(doc.id)
    assert stored.processing_status == "completed" and stored.processing_error is None
    assert stored.full_text == "Repaired policy"


async def test_job_cannot_read_other_org_or_other_storage_key(db_session, monkeypatch):
    user, doc, key = await setup_document(db_session, "local_org")
    monkeypatch.setattr(task, "db_client", db_session)
    download = AsyncMock(side_effect=AssertionError("must not read storage"))
    monkeypatch.setattr(task.storage_fs, "adownload_file", download)
    await task.process_knowledge_base_document(
        {}, doc.id, key, user.selected_organization_id + 1, "user"
    )
    await task.process_knowledge_base_document(
        {}, doc.id, key + "other", user.selected_organization_id, "user"
    )
    download.assert_not_awaited()
    assert (await db_session.get_document_by_id(doc.id)).processing_status == "pending"


async def test_duplicate_jobs_and_old_lease_cannot_replace_content(db_session):
    user, doc, key = await setup_document(db_session, "local_lease")
    org = user.selected_organization_id
    first = await db_session.claim_document_processing(doc.id, org, key)
    assert (
        first and await db_session.claim_document_processing(doc.id, org, key) is None
    )
    with pytest.raises(ValueError):
        await db_session.queue_document_retry(doc.document_uuid, org)
    assert await db_session.finish_document_processing(
        doc.id, org, first, error="failed"
    )
    await db_session.queue_document_retry(doc.document_uuid, org)
    second = await db_session.claim_document_processing(doc.id, org, key)
    assert second != first
    assert not await db_session.finish_document_processing(
        doc.id, org, first, full_text="stale"
    )
    assert await db_session.finish_document_processing(
        doc.id, org, second, full_text="current"
    )
    assert (await db_session.get_document_by_id(doc.id)).full_text == "current"


async def test_chunk_failure_keeps_served_index(db_session, monkeypatch):
    user, doc, key = await setup_document(db_session, "local_chunks", "chunked")
    org = user.selected_organization_id
    token = await db_session.claim_document_processing(doc.id, org, key)
    old = {
        "chunk_text": "old policy",
        "contextualized_text": "old policy",
        "chunk_index": 0,
        "chunk_metadata": {},
        "embedding_model": "old-model",
        "embedding_dimension": 1536,
        "embedding": [0.1] * 1536,
        "token_count": 10,
    }
    await db_session.finish_document_processing(
        doc.id, org, token, chunks=[old], metadata={"processor": "previous"}
    )
    await db_session.queue_document_retry(doc.document_uuid, org)
    monkeypatch.setattr(task, "db_client", db_session)

    async def download(key, path):
        from pathlib import Path

        Path(path).write_text("new policy")
        return True

    monkeypatch.setattr(task.storage_fs, "adownload_file", download)
    service = SimpleNamespace(embed_texts=AsyncMock(return_value=[[1.0, 2.0]]))
    monkeypatch.setattr(
        task, "prepare_embeddings", AsyncMock(return_value=(service, None))
    )
    await task.process_knowledge_base_document({}, doc.id, key, org, "user")
    stored = await db_session.get_document_by_id(doc.id)
    assert stored.processing_status == "failed" and "1536" in stored.processing_error
    assert stored.total_chunks == 1 and stored.docling_metadata == {
        "processor": "previous"
    }
    assert (await db_session.get_chunks_for_document(doc.id, org))[
        0
    ].chunk_text == "old policy"


async def test_upload_scopes_mode_validation_and_retry(
    db_session, test_client_factory, monkeypatch
):
    from api.routes import knowledge_base as routes

    user, doc, key = await setup_document(db_session, "local_api")
    enqueue = AsyncMock()
    monkeypatch.setattr(routes, "enqueue_document_processing", enqueue)
    async with test_client_factory(user) as client:
        for storage_key in (
            key.replace(f"/{user.selected_organization_id}/", "/99999/"),
            key + "/../rules.txt",
        ):
            response = await client.post(
                "/api/v1/knowledge-base/process-document",
                json={"document_uuid": doc.document_uuid, "s3_key": storage_key},
            )
            assert response.status_code == 422
        response = await client.post(
            "/api/v1/knowledge-base/process-document",
            json={
                "document_uuid": doc.document_uuid,
                "s3_key": key,
                "retrieval_mode": "unknown",
            },
        )
        assert response.status_code == 422
        response = await client.post(
            f"/api/v1/knowledge-base/documents/{doc.document_uuid}/retry"
        )
        assert response.status_code == 409
        await db_session.update_document_status(doc.id, "failed", error_message="test")
        response = await client.post(
            f"/api/v1/knowledge-base/documents/{doc.document_uuid}/retry"
        )
        assert response.status_code == 200
    enqueue.assert_awaited_once()


async def test_retry_commit_expiration_and_stranded_pending(db_session, db_connection):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from api.db.models import KnowledgeBaseDocumentModel

    user, doc, _ = await setup_document(db_session, "retry_expiration")
    org, doc_id, uid = user.selected_organization_id, doc.id, doc.document_uuid
    original = db_session.async_session
    db_session.async_session = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=True,
        join_transaction_mode="create_savepoint",
    )
    try:
        with pytest.raises(ValueError):
            await db_session.queue_document_retry(uid, org)
        async with db_session.async_session() as session, session.begin():
            await session.execute(
                update(KnowledgeBaseDocumentModel)
                .where(KnowledgeBaseDocumentModel.id == doc_id)
                .values(updated_at=datetime.now(UTC) - timedelta(seconds=301))
            )
        assert await db_session.queue_document_retry(uid, org + 999) is None
        retried = await db_session.queue_document_retry(uid, org)
        assert retried.id == doc_id and retried.processing_status == "pending"
        assert retried.custom_metadata["s3_key"]
        with pytest.raises(ValueError):
            await db_session.queue_document_retry(uid, org)
    finally:
        db_session.async_session = original
