import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings


UPLOAD_URL_EXPIRES = 15 * 60
LOCAL_FILE_MAX_SIZE = 20 * 1024 * 1024


class LocalFileStorageError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# 파일은 Django 서버를 거치지 않고 S3에 바로 올린다.
def create_upload_target(storage_key, mime_type):
    client = boto3.client('s3', region_name=settings.AWS_S3_REGION_NAME)

    return client.generate_presigned_url(
        'put_object',
        Params={
            'Bucket': settings.AWS_STORAGE_BUCKET_NAME,
            'Key': storage_key,
            'ContentType': mime_type,
        },
        ExpiresIn=UPLOAD_URL_EXPIRES,
    )


def delete_file(storage_key):
    client = boto3.client('s3', region_name=settings.AWS_S3_REGION_NAME)
    client.delete_object(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Key=storage_key,
    )


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
        raise LocalFileStorageError('storage_error') from exc
    except BotoCoreError as exc:
        raise LocalFileStorageError('storage_error') from exc
