"""Finding and fetching the daily drops in S3."""
from __future__ import annotations

import gzip
import io
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
