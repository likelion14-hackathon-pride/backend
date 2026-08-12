from django.shortcuts import render
import hmac, hashlib, time, json
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseForbidden
from django.conf import settings


def _verify(request):
    secret = settings.SLACK_SIGNING_SECRET
    if not secret:
        return False
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{request.body.decode()}"
    mine = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, request.headers.get("X-Slack-Signature", ""))


@csrf_exempt
def slack_events(request):
    if request.method != "POST":
        return JsonResponse({"ok": True})        # 헬스체크용

    if not _verify(request):
        return HttpResponseForbidden()

    data = json.loads(request.body)

    if data.get("type") == "url_verification":
        return JsonResponse({"challenge": data["challenge"]})

    event = data.get("event", {})
    if event.get("bot_id") or event.get("subtype"):
        return JsonResponse({"ok": True})

    print("받음:", event.get("text"), "|", event.get("user"))
    return JsonResponse({"ok": True})