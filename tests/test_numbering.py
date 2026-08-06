"""Сквозная нумерация результатов в Flow. Запуск: python tests/test_numbering.py

Требование: 1, 2, 3 — даже если третий отрывок перегенерировали дважды.
Номер закреплён за задачей, а не за порядком записей в журнале.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flowbatch.queue import RunLog

P = "proj-1"
OTHER = "proj-2"


def rec(job_id: str, project: str = P, status: str = "ok") -> str:
    return json.dumps({"id": job_id, "project": project, "status": status,
                       "url": f"x?name=uuid-{job_id}"}, ensure_ascii=False)



def check_policy(failures: list[str]) -> None:
    """Кого переименовываем и во что.

    Требование: имя плитки = id задачи, и только у видео. Картинки —
    промежуточные кадры, они нужны как референс и в монтаж не идут.
    """
    from types import SimpleNamespace

    from flowbatch.config import Config
    from flowbatch.runner import Runner

    cfg = Config.load(Path(__file__).resolve().parents[1] / "config.yaml")
    calls: list[tuple[str, str]] = []

    class Client:
        def rename_media(self, uuid, name):
            calls.append((uuid, name))
            return name

    r = Runner.__new__(Runner)
    r.cfg = cfg
    r.client = Client()
    r.log = None
    r.project_id = "p"
    r.console = SimpleNamespace(print=lambda *a, **k: None)

    item = SimpleNamespace(name="uuid-1")
    r._rename_result(item, SimpleNamespace(id="sahdom_p01_08_cliffhanger_anim", kind="video"))
    r._rename_result(item, SimpleNamespace(id="sahdom_p01_08_cliffhanger", kind="image"))

    if len(calls) != 1:
        failures.append(f"переименований {len(calls)}, ждали 1 (только видео)")
        return
    if calls[0][1] != "sahdom_p01_08_cliffhanger_anim":
        failures.append(f"видео названо {calls[0][1]!r}, ждали id задачи")


def main() -> int:
    failures: list[str] = []
    check_policy(failures)
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "runs.jsonl"
        log = RunLog(path)

        # Пустой журнал: первая задача получает единицу.
        if log.sequence_for("a", project=P) != 1:
            failures.append("на пустом журнале номер не 1")

        # Два успеха подряд -> третья задача получает 3.
        path.write_text("\n".join([rec("a"), rec("b")]) + "\n", encoding="utf-8")
        if log.sequence_for("c", project=P) != 3:
            failures.append(f"третья задача получила {log.sequence_for('c', project=P)}")

        # Главное: третью перегенерировали ДВАЖДЫ — номер остаётся 3.
        path.write_text("\n".join([rec("a"), rec("b"), rec("c"), rec("c"), rec("c")]) + "\n",
                        encoding="utf-8")
        for name, expected in (("a", 1), ("b", 2), ("c", 3)):
            got = log.sequence_for(name, project=P)
            if got != expected:
                failures.append(f"после перегенераций {name}: ожидалось {expected}, вышло {got}")
        # И следующая новая задача — 4, а не 6.
        if log.sequence_for("d", project=P) != 4:
            failures.append(f"новая задача после перегенераций: {log.sequence_for('d', project=P)}")

        # Неудачные попытки в счёт не идут.
        path.write_text("\n".join([rec("a"), rec("x", status="failed"), rec("b")]) + "\n",
                        encoding="utf-8")
        if log.sequence_for("b", project=P) != 2:
            failures.append("проваленная задача заняла номер")

        # Нумерация своя в каждом проекте.
        path.write_text("\n".join([rec("a"), rec("b"), rec("z", project=OTHER)]) + "\n",
                        encoding="utf-8")
        if log.sequence_for("z", project=OTHER) != 1:
            failures.append(f"чужой проект влияет на номер: {log.sequence_for('z', project=OTHER)}")
        if log.sequence_for("c", project=P) != 3:
            failures.append("чужой проект сбил нумерацию своего")

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: нумерация 1,2,3 держится при перегенерациях и не течёт между проектами")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
