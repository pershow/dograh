"""Private attachment storage and resource-bounded local parsing, without MPS."""

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import uuid
from pathlib import Path

from api.db import db_client
from api.services.agent_workbench.errors import WorkbenchError
from api.services.documents.parsing import FileParsingError, parse_local_file

MAX_BYTES = 20 * 1024 * 1024
ALLOWED = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".pdf",
    ".docx",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}


async def read_selected_attachment(
    org,
    session_id,
    selected_ids,
    asset_id,
    offset=0,
    limit=24000,
    *,
    from_end=False,
    query=None,
):
    """Read a ready asset from the caller's scoped session-library allowlist."""
    if asset_id not in selected_ids:
        raise WorkbenchError("Attachment was not selected for this task")
    if (
        type(offset) is not int
        or type(limit) is not int
        or offset < 0
        or not 1 <= limit <= 24000
        or type(from_end) is not bool
        or (
            query is not None
            and (not isinstance(query, str) or not 1 <= len(query) <= 200)
        )
        or (query is not None and from_end)
    ):
        raise WorkbenchError(
            "Attachment offset must be nonnegative and limit between 1 and 24000"
        )
    asset = await db_client.wb_get(org, session_id, "assets", asset_id)
    if asset["status"] != "ready":
        raise WorkbenchError("附件尚未解析完成。")
    parsed = asset["parsed"]
    if parsed.get("kind") == "image":
        return {
            "name": asset["name"],
            "kind": "image",
            "width": parsed["width"],
            "height": parsed["height"],
        }
    if parsed.get("truncated") and "full_text" not in parsed:
        # The preview remains bounded. On-demand pages use the original file,
        # never the old preview's silently truncated tail.
        with tempfile.TemporaryDirectory(prefix="workbench-full-text-") as directory:
            local = Path(directory) / ("source" + Path(asset["name"]).suffix.lower())
            await PrivateAssetStore().download(asset["object_key"], local)
            if (
                local.stat().st_size > MAX_BYTES
                or hashlib.sha256(local.read_bytes()).hexdigest() != asset["sha256"]
            ):
                raise WorkbenchError("Attachment integrity check failed")
            complete = await parse_local_file(local, full_text=True)
        parsed = {**parsed, "full_text": complete.get("text", "")}
        await db_client.wb_asset_result(org, session_id, asset_id, parsed=parsed)
    text = parsed.get("full_text", parsed.get("text", ""))
    if from_end:
        offset = max(0, len(text) - offset - limit)
    if query is not None:
        # Literal search, never user-supplied regular expressions. Return one
        # bounded source page; the next exact match remains directly seekable.
        match = re.compile(re.escape(query), re.IGNORECASE).search(text, offset)
        if match is None:
            return {
                "name": asset["name"],
                "kind": "text",
                "text": "",
                "offset": offset,
                "next_offset": None,
                "total_characters": len(text),
                "query": query,
                "found": False,
            }
        offset = max(offset, match.start() - min(limit // 4, 500))
    end = min(offset + limit, len(text))
    return {
        "name": asset["name"],
        "kind": "text",
        "text": text[offset:end],
        "offset": offset,
        "next_offset": end if end < len(text) else None,
        "total_characters": len(text),
        "truncated_by_parser": parsed.get("truncated", False)
        and "full_text" not in parsed,
        "source_locations": [s["location"] for s in parsed.get("segments", [])],
        **(
            {
                "query": query,
                "found": True,
                "match_offset": match.start(),
                "next_match_offset": match.end(),
            }
            if query is not None
            else {}
        ),
    }


class PrivateAssetStore:
    """Do not reuse MinioFileSystem: it grants public bucket access in local mode."""

    def __init__(self):
        from api.constants import (
            ENABLE_AWS_S3,
            MINIO_ACCESS_KEY,
            MINIO_BUCKET,
            MINIO_ENDPOINT,
            MINIO_SECRET_KEY,
            MINIO_SECURE,
            S3_BUCKET,
        )

        self.s3 = ENABLE_AWS_S3
        self.bucket = os.getenv(
            "WORKBENCH_ASSET_BUCKET",
            (S3_BUCKET if self.s3 else MINIO_BUCKET) + "-workbench",
        )
        if self.s3:
            from api.constants import (
                S3_ADDRESSING_STYLE,
                S3_ENDPOINT_URL,
                S3_REGION,
                S3_SIGNATURE_VERSION,
            )
            from api.services.filesystem.s3 import S3FileSystem

            self.fs = S3FileSystem(
                bucket_name=self.bucket,
                region_name=S3_REGION,
                endpoint_url=S3_ENDPOINT_URL,
                signature_version=S3_SIGNATURE_VERSION,
                addressing_style=S3_ADDRESSING_STYLE,
            )
        else:
            from minio import Minio

            self.client = Minio(
                MINIO_ENDPOINT,
                access_key=MINIO_ACCESS_KEY,
                secret_key=MINIO_SECRET_KEY,
                secure=MINIO_SECURE,
            )

    async def put(self, key, data, content_type):
        if self.s3:
            from botocore.exceptions import ClientError

            async with self.fs.session.client(
                "s3", **self.fs._client_kwargs()
            ) as client:
                try:
                    policy = await client.get_bucket_policy(Bucket=self.bucket)
                    if json.loads(policy.get("Policy", "{}")).get("Statement"):
                        raise WorkbenchError(
                            "Workbench attachment bucket must have no public policy",
                            503,
                        )
                except ClientError as error:
                    if error.response["Error"]["Code"] != "NoSuchBucketPolicy":
                        raise WorkbenchError(
                            "Cannot verify private attachment bucket policy", 503
                        ) from error
            if not await self.fs.acreate_file_from_bytes(key, data):
                raise WorkbenchError("Attachment storage failed", 503)
            return
        import io

        from minio.error import S3Error

        def upload():
            if not self.client.bucket_exists(self.bucket):
                try:
                    self.client.make_bucket(self.bucket)
                except S3Error as error:
                    if error.code != "BucketAlreadyOwnedByYou":
                        raise
            # Fail closed if an operator accidentally configured a public bucket.
            try:
                policy = self.client.get_bucket_policy(self.bucket)
                if policy and json.loads(policy).get("Statement"):
                    raise WorkbenchError(
                        "Workbench attachment bucket must have no public policy", 503
                    )
            except S3Error as error:
                if error.code != "NoSuchBucketPolicy":
                    raise
            self.client.put_object(
                self.bucket, key, io.BytesIO(data), len(data), content_type=content_type
            )

        await asyncio.to_thread(upload)

    async def download(self, key, destination):
        if self.s3:
            if not await self.fs.adownload_file(key, str(destination)):
                raise WorkbenchError("Attachment could not be read", 503)
        else:
            await asyncio.to_thread(
                self.client.fget_object, self.bucket, key, str(destination)
            )


async def upload_attachment(org, session_id, upload):
    await db_client.wb_context(org, session_id)
    name = Path((upload.filename or "attachment").replace("\\", "/")).name[:255]
    if Path(name).suffix.lower() not in ALLOWED:
        raise WorkbenchError("Unsupported attachment format", 415)
    data = await upload.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise WorkbenchError("Attachment must be between 1 byte and 20 MiB", 413)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    key = f"organizations/{org}/workbench/{session_id}/{uuid.uuid4()}"
    await PrivateAssetStore().put(key, data, content_type)
    asset = await db_client.wb_asset(
        org,
        session_id,
        name=name,
        content_type=content_type,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        object_key=key,
    )
    # The parser runs out of process; the API request is bounded and never runs document code.
    return await parse_attachment(org, session_id, asset["id"])


async def parse_attachment(org, session_id, asset_id):
    asset = await db_client.wb_get(org, session_id, "assets", asset_id)
    try:
        with tempfile.TemporaryDirectory(prefix="dograh-asset-") as directory:
            local = Path(directory) / ("input" + Path(asset["name"]).suffix.lower())
            await PrivateAssetStore().download(asset["object_key"], local)
            if (
                local.stat().st_size > MAX_BYTES
                or hashlib.sha256(local.read_bytes()).hexdigest() != asset["sha256"]
            ):
                raise WorkbenchError("Attachment integrity check failed")
            parsed = await parse_local_file(local)
        return await db_client.wb_asset_result(org, session_id, asset_id, parsed=parsed)
    except Exception as error:  # noqa: BLE001 - boundary converts failures to bounded task status
        message = (
            str(error)
            if isinstance(error, (WorkbenchError, FileParsingError))
            else "Attachment parsing failed; retry or use a text document"
        )
        return await db_client.wb_asset_result(org, session_id, asset_id, error=message)
