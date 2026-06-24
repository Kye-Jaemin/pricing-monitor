"""번들 구성요소 정가가 왜 합산에 안 들어가는지 진단(CLI).

사용법(앱을 띄우는 것과 '같은 폴더/같은 방식'으로 실행):
    python -m scripts.diag_bundle_price mybox
웹에서도 동일 결과: /diag/bundle-price?q=mybox
"""
import sys

from app.core import presenters


def main() -> None:
    q = sys.argv[1] if len(sys.argv) > 1 else "mybox"
    print(presenters.diag_bundle_price(q))


if __name__ == "__main__":
    main()
