import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from config.errors import STORAGE_UNAVAILABLE, UpstreamError


UPLOAD_URL_EXPIRES = 15 * 60
LOCAL_FILE_MAX_SIZE = 20 * 1024 * 1024


# S3가 응답하지 않거나 올라온 파일이 약속과 다른 경우.
# 사용자가 고칠 수 있는 일이 아니므로 503으로 나간다.
class LocalFileStorageError(UpstreamError):
    pass


# 파일은 Django 서버를 거치지 않고 S3에 바로 올린다.
def create_upload_target(storage_key, mime_type):
    client = boto3.client('s3', region_name=settings.AWS_S3_REGION_NAME)

    try:
        return client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': settings.AWS_STORAGE_BUCKET_NAME,
                'Key': storage_key,
                'ContentType': mime_type,
            },
            ExpiresIn=UPLOAD_URL_EXPIRES,
        )
    except (BotoCoreError, ClientError) as exc:
        raise LocalFileStorageError(STORAGE_UNAVAILABLE) from exc


# 업로드는 브라우저가 S3에 직접 한다. 서버는 끝났다는 통보를 받지 못하므로
# 큐에 넣기 전에 파일이 실제로 올라왔는지 여기서 확인한다.
def object_exists(storage_key):
    client = boto3.client('s3', region_name=settings.AWS_S3_REGION_NAME)

    try:
        client.head_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME,
            Key=storage_key,
        )
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        if code in {'NoSuchKey', 'NotFound', '404'}:
            return False
        raise LocalFileStorageError(STORAGE_UNAVAILABLE) from exc
    except BotoCoreError as exc:
        raise LocalFileStorageError(STORAGE_UNAVAILABLE) from exc

    return True


def delete_file(storage_key):
    client = boto3.client('s3', region_name=settings.AWS_S3_REGION_NAME)

    try:
        client.delete_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME,
            Key=storage_key,
        )
    except (BotoCoreError, ClientError) as exc:
        raise LocalFileStorageError(STORAGE_UNAVAILABLE) from exc


def download_file(storage_key, expected_size):
    client = boto3.client('s3', region_name=settings.AWS_S3_REGION_NAME)

    try:
        response = client.get_object(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME,
            Key=storage_key,
        )
        actual_size = response['ContentLength']
        if actual_size != expected_size:
            raise LocalFileStorageError('file_size_mismatch')
        if actual_size > LOCAL_FILE_MAX_SIZE:
            raise LocalFileStorageError('file_too_large')

        return response['Body'].read(LOCAL_FILE_MAX_SIZE + 1)
    except LocalFileStorageError:
        raise
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code')
        if code in {'NoSuchKey', '404'}:
            raise LocalFileStorageError('file_not_uploaded') from exc
        raise LocalFileStorageError(STORAGE_UNAVAILABLE) from exc
    except BotoCoreError as exc:
        raise LocalFileStorageError(STORAGE_UNAVAILABLE) from exc
