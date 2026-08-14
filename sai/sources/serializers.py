from rest_framework import serializers

from .models import Connection, Item


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
