"""Finding and fetching the daily drops in S3."""
from __future__ import annotations

import gzip
import io
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass

import boto3

from config import get_settings

SUFFIXES = (".csv", ".csv.gz", ".csv.zip", ".zip")


@dataclass(frozen=True)
class S3Object:
    key: str
    etag: str
    size: int


def client():
    settings = get_settings()
    return boto3.client("s3", region_name=settings.aws_region)


def list_objects(bucket: str | None = None, prefix: str | None = None) -> list[S3Object]:
    """Every CSV drop under the prefix, oldest key first."""
    settings = get_settings()
    bucket = bucket or settings.s3_bucket
    prefix = settings.s3_prefix if prefix is None else prefix

    found: list[S3Object] = []
    paginator = client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith("/") or not key.lower().endswith(SUFFIXES):
                continue
            found.append(
                S3Object(key=key, etag=item.get("ETag", "").strip('"'), size=item["Size"])
            )
    return sorted(found, key=lambda o: o.key)


def fetch_csv_file(key: str, bucket: str | None = None) -> str:
    """Download one drop to a temp file and unwrap it there.

    Never through memory: a 69MB drop costs ~180MB as bytes plus a parsed
    frame on top, which does not fit beside a web worker on a small
    instance. The caller deletes the file.
    """
    settings = get_settings()
    bucket = bucket or settings.s3_bucket
    lowered = key.lower()

    raw = tempfile.NamedTemporaryFile(suffix=".raw", delete=False)
    try:
        client().download_fileobj(bucket, key, raw)
        raw.close()

        if not lowered.endswith((".gz", ".zip")):
            return raw.name

        out = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        try:
            if lowered.endswith(".gz"):
                with gzip.open(raw.name, "rb") as source:
                    shutil.copyfileobj(source, out)
            else:
                with zipfile.ZipFile(raw.name) as archive:
                    names = [
                        n for n in archive.namelist()
                        # Zips made on a Mac carry a __MACOSX/._name shadow.
                        if n.lower().endswith(".csv") and not n.startswith("__MACOSX/")
                    ]
                    if not names:
                        raise ValueError(f"{key} contains no CSV")
                    with archive.open(names[0]) as source:
                        shutil.copyfileobj(source, out)
            out.close()
            return out.name
        except Exception:
            out.close()
            os.unlink(out.name)
            raise
    finally:
        if lowered.endswith((".gz", ".zip")) and os.path.exists(raw.name):
            os.unlink(raw.name)


def fetch_csv_bytes(key: str, bucket: str | None = None) -> bytes:
    """Download one drop and unwrap it if it arrived compressed."""
    settings = get_settings()
    bucket = bucket or settings.s3_bucket
    body = client().get_object(Bucket=bucket, Key=key)["Body"].read()

    lowered = key.lower()
    if lowered.endswith(".gz"):
        return gzip.decompress(body)
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = [
                n for n in archive.namelist()
                # Zips made on a Mac carry a __MACOSX/._name shadow copy.
                if n.lower().endswith(".csv") and not n.startswith("__MACOSX/")
            ]
            if not names:
                raise ValueError(f"{key} contains no CSV")
            return archive.read(names[0])
    return body
