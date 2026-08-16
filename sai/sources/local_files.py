import boto3
from django.conf import settings


UPLOAD_URL_EXPIRES = 15 * 60


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
