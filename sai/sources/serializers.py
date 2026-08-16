from pathlib import Path

from rest_framework import serializers

from handbook.models import CompanyScope

from .local_files import LOCAL_FILE_MAX_SIZE
from .models import Connection, IngestionJob, Item


LOCAL_FILE_EXTENSIONS = {'.txt', '.md', '.pdf', '.docx'}
LOCAL_FILE_MIME_TYPES = {
    '.txt': {'text/plain'},
    '.md': {'text/markdown', 'text/plain'},
    '.pdf': {'application/pdf'},
    '.docx': {'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
}


class ChannelSerializer(serializers.ModelSerializer):
    externalId = serializers.CharField(source='external_id', read_only=True)
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True, default=None)
    scopeKind = serializers.CharField(source='scope.kind', read_only=True, default=None)
    isScopeConfirmed = serializers.BooleanField(source='is_scope_confirmed', read_only=True)
    itemCount = serializers.IntegerField(source='item_count', read_only=True)
    lastSyncedAt = serializers.DateTimeField(source='last_synced_at', read_only=True)

    class Meta:
        model = Item
        fields = [
            'id',
            'externalId',
            'label',
            'scopeId',
            'scopeName',
            'scopeKind',
            'isScopeConfirmed',
            'itemCount',
            'lastSyncedAt',
        ]


class ChannelListSerializer(serializers.Serializer):
    items = ChannelSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 아직 등록되지 않은 워크스페이스 채널
class AvailableChannelSerializer(serializers.Serializer):
    externalId = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    isPrivate = serializers.BooleanField(read_only=True)
    isMember = serializers.BooleanField(read_only=True)


class AvailableChannelListSerializer(serializers.Serializer):
    items = AvailableChannelSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class ChannelAddSerializer(serializers.Serializer):
    externalId = serializers.CharField(trim_whitespace=True)


# 채널을 지식공간에 연결
# 여기서 정한 범위가 나중에 이 채널에서 뽑은 규칙 초안의 기본 범위가 됨
class ChannelScopeUpdateSerializer(serializers.Serializer):
    scopeId = serializers.PrimaryKeyRelatedField(
        source='scope',
        queryset=CompanyScope.objects.all(),
        allow_null=True,
    )

    def validate_scopeId(self, value):
        if value is not None and value.company_id != self.context['company'].id:
            raise serializers.ValidationError('scope not found')

        return value

    def update(self, instance, validated_data):
        instance.scope = validated_data['scope']
        # 대표가 직접 지정한 것이므로 확정으로 표시한다. 해제하면 미확정으로 되돌린다.
        instance.is_scope_confirmed = instance.scope is not None
        instance.save(update_fields=['scope', 'is_scope_confirmed'])

        return instance


# GitHub 수집 대상 레포 응답용 시리얼라이저
class RepositorySerializer(serializers.ModelSerializer):
    externalId = serializers.CharField(source='external_id', read_only=True)
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True, default=None)
    scopeKind = serializers.CharField(source='scope.kind', read_only=True, default=None)
    isScopeConfirmed = serializers.BooleanField(source='is_scope_confirmed', read_only=True)
    itemCount = serializers.IntegerField(source='item_count', read_only=True)
    lastSyncedAt = serializers.DateTimeField(source='last_synced_at', read_only=True)

    class Meta:
        model = Item
        fields = [
            'id',
            'externalId',
            'label',
            'scopeId',
            'scopeName',
            'scopeKind',
            'isScopeConfirmed',
            'itemCount',
            'lastSyncedAt',
        ]


class RepositoryListSerializer(serializers.Serializer):
    items = RepositorySerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# GitHub App이 접근 가능하지만 아직 수집 대상으로 등록하지 않은 레포
class AvailableRepositorySerializer(serializers.Serializer):
    externalId = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    isPrivate = serializers.BooleanField(read_only=True)


class AvailableRepositoryListSerializer(serializers.Serializer):
    items = AvailableRepositorySerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class RepositoryAddSerializer(serializers.Serializer):
    externalId = serializers.CharField(trim_whitespace=True)


# 레포도 채널과 같이 기존 회사/프로젝트 범위 하나에 연결한다.
class RepositoryScopeUpdateSerializer(ChannelScopeUpdateSerializer):
    pass


# 로컬 파일 업로드 메타데이터
class LocalFileUploadCreateSerializer(serializers.Serializer):
    fileName = serializers.CharField(max_length=200, trim_whitespace=True)
    mimeType = serializers.CharField(max_length=100, trim_whitespace=True)
    size = serializers.IntegerField(min_value=1, max_value=LOCAL_FILE_MAX_SIZE)

    def validate_fileName(self, value):
        if Path(value).name != value:
            raise serializers.ValidationError('file name is invalid')
        if Path(value).suffix.lower() not in LOCAL_FILE_EXTENSIONS:
            raise serializers.ValidationError('file type is not supported')

        return value

    def validate(self, attrs):
        extension = Path(attrs['fileName']).suffix.lower()
        if attrs['mimeType'] not in LOCAL_FILE_MIME_TYPES[extension]:
            raise serializers.ValidationError({
                'mimeType': ['mime type does not match file extension'],
            })

        return attrs


class LocalFileSerializer(serializers.ModelSerializer):
    fileName = serializers.CharField(source='label', read_only=True)
    mimeType = serializers.CharField(source='mime_type', read_only=True)
    size = serializers.IntegerField(source='byte_size', read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = ['id', 'fileName', 'mimeType', 'size', 'status']

    def get_status(self, obj):
        latest_job = (
            IngestionJob.objects.filter(
                company_id=obj.company_id,
                item_ids__contains=[obj.id],
            )
            .order_by('-id')
            .first()
        )
        if latest_job and latest_job.status in {
            IngestionJob.Status.QUEUED,
            IngestionJob.Status.RUNNING,
        }:
            return 'PROCESSING'

        if latest_job and latest_job.status == IngestionJob.Status.FAILED:
            return 'ERROR'

        if latest_job and latest_job.status == IngestionJob.Status.PARTIAL:
            item_failed = any(
                error.get('itemId') == obj.id
                for error in latest_job.errors or []
            )
            if item_failed:
                return 'ERROR'

        if obj.last_synced_at:
            return 'READY'

        return 'PENDING_UPLOAD'


class LocalFileListSerializer(serializers.Serializer):
    items = LocalFileSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class LocalFileUploadResultSerializer(serializers.Serializer):
    sourceFile = LocalFileSerializer(read_only=True)
    uploadTarget = serializers.URLField(read_only=True)


class IngestionJobSerializer(serializers.ModelSerializer):
    itemIds = serializers.JSONField(source='item_ids', read_only=True)
    documentCount = serializers.SerializerMethodField()
    candidateCount = serializers.IntegerField(source='entry_count', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    startedAt = serializers.DateTimeField(source='started_at', read_only=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True)

    class Meta:
        model = IngestionJob
        fields = [
            'id',
            'kind',
            'status',
            'progress',
            'itemIds',
            'documentCount',
            'candidateCount',
            'errors',
            'createdAt',
            'startedAt',
            'completedAt',
        ]

    # 이 작업이 대상으로 삼은 채널들이 지금까지 모아 둔 원문 수
    def get_documentCount(self, obj):
        return sum(
            Item.objects.filter(id__in=obj.item_ids or []).values_list('item_count', flat=True)
        )


class IngestionJobListSerializer(serializers.Serializer):
    items = IngestionJobSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 수집 작업 시작 요청. provider를 생략하면 기존과 같이 Slack을 수집한다.
class IngestionJobCreateSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(
        choices=[Connection.Kind.SLACK, Connection.Kind.GITHUB, Connection.Kind.LOCAL],
        required=False,
        default=Connection.Kind.SLACK,
    )
    itemIds = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=False,
        help_text=(
            '수집할 채널, 레포 또는 로컬 파일 ID 목록. '
            '생략하면 해당 소스에 등록된 전체 Item이 대상입니다.'
        ),
    )


# 토큰과 시그닝 시크릿은 어떤 경우에도 응답에 넣지 않는다.
class ConnectionSerializer(serializers.ModelSerializer):
    provider = serializers.CharField(source='kind', read_only=True)
    displayName = serializers.CharField(source='display_name', read_only=True)
    workspaceId = serializers.CharField(source='external_workspace_id', read_only=True)
    errorMessage = serializers.CharField(source='error_message', read_only=True)
    resourceCount = serializers.SerializerMethodField()
    lastSyncedAt = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Connection
        fields = [
            'id',
            'provider',
            'status',
            'displayName',
            'workspaceId',
            'errorMessage',
            'resourceCount',
            'lastSyncedAt',
            'createdAt',
        ]

    def get_resourceCount(self, obj):
        return obj.items.filter(removed_at__isnull=True).count()

    def get_lastSyncedAt(self, obj):
        latest = obj.items.filter(last_synced_at__isnull=False).order_by('-last_synced_at').first()

        return latest.last_synced_at if latest else None


# GitHub 연결 직후 5단계 화면에서만 사용한다.
class GitHubConnectionResultSerializer(ConnectionSerializer):
    webhookUrl = serializers.SerializerMethodField()
    webhookSecret = serializers.CharField(source='github_webhook_secret', read_only=True)

    class Meta(ConnectionSerializer.Meta):
        fields = ConnectionSerializer.Meta.fields + ['webhookUrl', 'webhookSecret']

    def get_webhookUrl(self, obj):
        request = self.context.get('request')
        if request is None:
            return '/api/github/events/'

        return request.build_absolute_uri('/api/github/events/')


class ConnectionListSerializer(serializers.Serializer):
    items = ConnectionSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 토큰 유효성은 슬랙 auth.test로 확인해야 하므로 뷰에서 검증한다.
class SlackConnectionCreateSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=[Connection.Kind.SLACK])
    botToken = serializers.CharField(write_only=True, trim_whitespace=True)
    signingSecret = serializers.CharField(write_only=True, trim_whitespace=True)

    def validate_botToken(self, value):
        # 붙여넣기 실수를 슬랙 호출 전에 걸러 낸다.
        if not value.startswith('xoxb-'):
            raise serializers.ValidationError('bot token must start with xoxb-')

        return value


# 대표가 GitHub App 연결 화면에서 입력하는 값.
class GitHubConnectionCreateSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=[Connection.Kind.GITHUB])
    appId = serializers.CharField(trim_whitespace=True, write_only=True)
    installationId = serializers.CharField(trim_whitespace=True, write_only=True)
    privateKey = serializers.FileField(write_only=True)

    def validate_appId(self, value):
        if not value.isdigit():
            raise serializers.ValidationError('app id must be a number')

        return value

    def validate_installationId(self, value):
        if not value.isdigit():
            raise serializers.ValidationError('installation id must be a number')

        return value

    def validate_privateKey(self, value):
        if value.size > 64 * 1024:
            raise serializers.ValidationError('private key file is too large')

        try:
            private_key = value.read().decode('utf-8').strip()
        except UnicodeDecodeError as exc:
            raise serializers.ValidationError('private key must be a PEM file') from exc

        if '-----BEGIN' not in private_key or 'PRIVATE KEY-----' not in private_key:
            raise serializers.ValidationError('private key must be a PEM file')

        return private_key + '\n'
