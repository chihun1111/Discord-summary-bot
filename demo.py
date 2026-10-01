"""No token, SDK, AI call or persistent chat storage is needed for this demo."""
from pathlib import Path
import tempfile
import time

from chatbot.store import Record, Store


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "demo.db")
        now = time.time()
        samples = [
            Record(1001, 1, 10, 101, "민수", "배포일정은 내일 오후 3시로 확정합니다.", now, now),
            Record(1002, 1, 10, 102, "지연", "결제 오류 수정은 제가 담당할게요.", now + 1, now + 1),
            Record(1003, 1, 20, 103, "비공개", "배포 관련 비공개 회의 내용", now + 2, now + 2),
        ]
        store.upsert(samples)
        for query in ("배포", "배포 일정", "결제"):
            hits = store.search(1, [10], query, since=0)
            print(f"검색: {query!r}, 읽기 허용 채널: 10")
            for hit in hits:
                print(f"  [{hit.message_id}] {hit.author_name}: {hit.content}")
        store.delete([1001])
        print("1001 삭제 후 '배포' 검색:", len(store.search(1, [10], "배포", 0)), "개")
        print("모든 데이터는 가상이며 임시 DB는 종료 시 제거됩니다.")


if __name__ == "__main__":
    main()
