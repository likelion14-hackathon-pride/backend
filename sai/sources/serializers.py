from rest_framework import serializers

from handbook.models import CompanyScope

from .models import Connection, IngestionJob, Item


# 수집 대상 채널 응답용 시리얼라이저
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


# 아직 등록되지 않은 워크스페이스 채널
class AvailableChannelSerializer(serializers.Serializer):
    externalId = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    isPrivate = serializers.BooleanField(read_only=True)
    isMember = serializers.BooleanField(read_only=True)


class AvailableChannelListSerializer(serializers.Serializer):
    items = AvailableChannelSerializer(many=True)


class ChannelAddSerializer(serializers.Serializer):
    externalId = serializers.CharField(trim_whitespace=True)


# 채널을 지식공간에 연결한다.
# 여기서 정한 범위가 나중에 이 채널에서 뽑은 규칙 초안의 기본 범위가 된다.
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


# 수집 작업 응답용 시리얼라이저
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

    # 이 작업이 대상으로 삼은 채널들이 지금까지 모아 둔 원문 수.
    def get_documentCount(self, obj):
        return sum(
            Item.objects.filter(id__in=obj.item_ids or []).values_list('item_count', flat=True)
        )


class IngestionJobListSerializer(serializers.Serializer):
    items = IngestionJobSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 수집 작업 시작 요청.
# itemIds를 생략하면 수집 대상 채널 전체를 대상으로 한다.
class IngestionJobCreateSerializer(serializers.Serializer):
    itemIds = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=False,
        help_text=(
            '수집할 채널 ID 목록. 생략하면 등록된 채널 전체가 대상입니다. '
            'ID는 GET /source-connections/{connectionId}/channels 로 확인하세요.'
        ),
    )


# 소스 연결 응답용 시리얼라이저.
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


class ConnectionListSerializer(serializers.Serializer):
    items = ConnectionSerializer(many=True)


# 슬랙 키 입력용 시리얼라이저.
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
