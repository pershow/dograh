"""Credential-free, time- and memory-bounded document parsing subprocess."""

import asyncio
import json
import os
import sys


class FileParsingError(ValueError):
    pass


async def parse_local_file(path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "api.services.documents.file_parser",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={
            key: os.environ[key]
            for key in ("PATH", "LANG", "SYSTEMROOT")
            if key in os.environ
        },
    )
    try:
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError as error:
            raise FileParsingError(
                "Document parsing exceeded the 30-second limit"
            ) from error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise FileParsingError(
            "File is malformed, encrypted, or exceeds parsing limits"
        )
    parsed = json.loads(stdout)
    if parsed.get("error"):
        raise FileParsingError(parsed["error"])
    return parsed
