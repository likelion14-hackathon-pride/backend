import faulthandler
import signal

from django.core.management.base import BaseCommand

from sources.worker import IDLE_SECONDS, drain, work_forever


class Command(BaseCommand):
    help = '수집 작업 큐를 처리한다. 배포 환경에서는 systemd 서비스로 상시 실행한다.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='큐에 쌓인 것만 처리하고 종료한다. 로컬 확인이나 cron 용.',
        )
        parser.add_argument(
            '--interval',
            type=float,
            default=IDLE_SECONDS,
            help=f'큐가 비었을 때 다시 확인하기까지 대기 시간(초). 기본 {IDLE_SECONDS}',
        )

    def handle(self, *args, **options):
        if options['once']:
            processed = drain()
            self.stdout.write(self.style.SUCCESS(f'{processed}건 처리'))
            return

        # 워커가 CPU를 물고 놓지 않을 때 `kill -USR1 <pid>` 로 지금 어느 줄에서
        # 돌고 있는지 스택을 받는다. 로그가 없는 구간에서 추측 없이 지점을 짚는 유일한 방법이다.
        # SIGUSR1 은 윈도우에 없다. 로컬 개발에서는 조용히 넘어간다.
        if hasattr(signal, 'SIGUSR1'):
            faulthandler.register(signal.SIGUSR1)
            self.stdout.write('kill -USR1 로 스택을 덤프할 수 있습니다.')

        self.stdout.write(f'큐 대기 중 (간격 {options["interval"]}초). Ctrl+C 로 종료.')
        try:
            work_forever(idle_seconds=options['interval'])
        except KeyboardInterrupt:
            self.stdout.write('\n종료')
