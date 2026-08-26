import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


class S3Client:
    """Client for uploading voice recordings to S3-compatible storage."""

    def __init__(self):
        self.endpoint_url = os.getenv("S3_ENDPOINT", "https://s3.regru.cloud")
        self.access_key = os.getenv("S3_ACCESS_KEY")
        self.secret_key = os.getenv("S3_SECRET_KEY")
        self.bucket = os.getenv("S3_BUCKET", "shelter-bot-voice")
        self.region = os.getenv("S3_REGION", "ru-central1")

        self._client = None

    def _get_client(self):
        """Lazy initialization of S3 client."""
        if self._client is None:
            if not self.access_key or not self.secret_key:
                raise NoCredentialsError("S3 credentials not configured")

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
            )
        return self._client

    def _generate_s3_key(self, worker_id: int, channel_name: str, timestamp: datetime) -> str:
        """Generate S3 key for a recording."""
        date_path = timestamp.strftime("%Y/%m/%d")
        filename = f"worker-{worker_id}_{channel_name}_{timestamp.strftime('%Y%m%d_%H%M%S')}.ogg"
        return f"{date_path}/{filename}"

    def _generate_metadata_key(self, s3_key: str) -> str:
        """Generate S3 key for metadata JSON."""
        return s3_key.replace(".ogg", "_meta.json")

    async def upload_recording(
        self,
        audio_path: Path,
        worker_id: int,
        channel_name: str,
        metadata: dict[str, Any],
        max_retries: int = 3,
    ) -> dict[str, str] | None:
        """
        Upload a recording to S3.
        Returns dict with s3_key and metadata_s3_key, or None on failure.
        """
        if not audio_path.exists():
            logger.error(f"Audio file not found: {audio_path}")
            return None

        timestamp = datetime.fromisoformat(metadata["started_at"])
        s3_key = self._generate_s3_key(worker_id, channel_name, timestamp)
        metadata_key = self._generate_metadata_key(s3_key)

        client = self._get_client()

        # Upload audio file with retries
        for attempt in range(max_retries):
            try:
                client.upload_file(
                    str(audio_path),
                    self.bucket,
                    s3_key,
                    ExtraArgs={"ContentType": "audio/ogg"},
                )
                logger.info(f"Uploaded audio to s3://{self.bucket}/{s3_key}")
                break
            except ClientError as e:
                logger.error(f"Upload attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"Failed to upload audio after {max_retries} attempts")
                    return None

        # Upload metadata JSON
        metadata["s3_key"] = s3_key
        metadata_json = json.dumps(metadata, indent=2, ensure_ascii=False)

        for attempt in range(max_retries):
            try:
                client.put_object(
                    Bucket=self.bucket,
                    Key=metadata_key,
                    Body=metadata_json.encode("utf-8"),
                    ContentType="application/json",
                )
                logger.info(f"Uploaded metadata to s3://{self.bucket}/{metadata_key}")
                break
            except ClientError as e:
                logger.error(f"Metadata upload attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    logger.error(f"Failed to upload metadata after {max_retries} attempts")
                    # Audio was uploaded, so we still return the audio key
                    return {"s3_key": s3_key, "metadata_s3_key": None}

        return {"s3_key": s3_key, "metadata_s3_key": metadata_key}

    async def delete_recording(self, s3_key: str, max_retries: int = 3) -> bool:
        """Delete a recording from S3."""
        client = self._get_client()
        metadata_key = self._generate_metadata_key(s3_key)

        for attempt in range(max_retries):
            try:
                client.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        "Objects": [
                            {"Key": s3_key},
                            {"Key": metadata_key},
                        ]
                    },
                )
                logger.info(f"Deleted recording from S3: {s3_key}")
                return True
            except ClientError as e:
                logger.error(f"Delete attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    return False

        return False

    def get_presigned_url(self, s3_key: str, expiration: int = 3600) -> str | None:
        """Generate a presigned URL for downloading a recording."""
        try:
            client = self._get_client()
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": s3_key},
                ExpiresIn=expiration,
            )
            return url
        except ClientError as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            return None

    async def check_bucket_exists(self) -> bool:
        """Check if the S3 bucket exists and is accessible."""
        try:
            client = self._get_client()
            client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as e:
            error_code = int(e.response["Error"]["Code"])
            if error_code == 404:
                logger.error(f"Bucket {self.bucket} does not exist")
            elif error_code == 403:
                logger.error(f"Access denied to bucket {self.bucket}")
            else:
                logger.error(f"Error checking bucket: {e}")
            return False
        except NoCredentialsError:
            logger.error("S3 credentials not configured")
            return False

    async def create_bucket_if_not_exists(self) -> bool:
        """Create the S3 bucket if it doesn't exist."""
        try:
            client = self._get_client()
            client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as e:
            error_code = int(e.response["Error"]["Code"])
            if error_code == 404:
                try:
                    client.create_bucket(
                        Bucket=self.bucket,
                        CreateBucketConfiguration={"LocationConstraint": self.region},
                    )
                    logger.info(f"Created bucket {self.bucket}")
                    return True
                except ClientError as create_error:
                    logger.error(f"Failed to create bucket: {create_error}")
                    return False
            else:
                logger.error(f"Error checking bucket: {e}")
                return False
