#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
menu.py — интерактивное меню диагностики парковочной системы VW Polo Sedan.

Меню сделано на Python, а не в bat-файле, намеренно: командный процессор
Windows читает bat в системной кодировке, из-за чего русский текст
в скрипте превращается в мусор и строки выполняются как команды.
Здесь кодировка под полным контролем программы.
"""

import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from pdc_core import VERSION                     # noqa: E402
from pdc_diag import build_parser, load_config   # noqa: E402


def setup_console():
    """Приводит вывод к UTF-8 и не даёт программе падать из-за символов."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def run(argv):
    """Запускает режим программы с указанными аргументами."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print("\n[!] Неверные параметры.")
        return
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n[прервано]")
    except Exception as exc:                       # noqa: BLE001
        print(f"\n[!] Ошибка выполнения: {exc}")


def ask(prompt, default=""):
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return value or default


def pause():
    try:
        input("\nНажмите Enter для возврата в меню...")
    except (EOFError, KeyboardInterrupt):
        pass


class State:
    host = None
    port = None

    @classmethod
    def net_args(cls):
        if cls.host:
            return ["--host", cls.host, "--port", str(cls.port or 35000)]
        return []


def show_menu():
    clear()
    print("=" * 70)
    print(f"  ДИАГНОСТИКА ПАРКОВОЧНОЙ СИСТЕМЫ VW POLO SEDAN   версия {VERSION}")
    print("=" * 70)
    print()
    print("  Перед началом:")
    print("    - адаптер вставлен в разъём OBD под рулём слева")
    print("    - зажигание ВКЛЮЧЕНО, двигатель не запущен")
    print("    - планшет подключён к сети Wi-Fi адаптера")
    print()
    print("-" * 70)
    print("  1  Поиск блока            начинать всегда с этого")
    print("  2  Ошибки                 прочитать память ошибок")
    print("  3  Ошибки + стирание      отсеять старые ошибки")
    print("  4  Информация о блоке     номер запчасти, версия ПО")
    print("  5  Поиск живых данных     подобрать номера датчиков")
    print("  6  Слежение за данными    какой номер какому датчику")
    print("  7  ПРОВОКАЦИОННЫЙ ТЕСТ    главный режим, шевеление жгута")
    print()
    print("  10 ПОЛНЫЙ ОТЧЁТ           все блоки и все их ошибки")
    print("  11 Детальный опрос блока  выбор из списка, без ввода адресов")
    print("  12 ЦЕЛЕВОЙ ОПРОС          только парктроник, разбор ошибок")
    print("  13 СТЕРЕТЬ И ПЕРЕЧИТАТЬ   отсеять старые ошибки, достать коды")
    print("  14 МОНИТОР ОШИБОК         живой счётчик, поиск отключением")
    print("  14 МОНИТОР СЧЁТЧИКА       поиск канала отключением датчиков")
    print()
    print("  8  Проверка дома          без машины, за одну минуту")
    print("  9  Адрес адаптера         если отличается от стандартного")
    print("  0  Выход")
    print("-" * 70)
    if State.host:
        print(f"  Адрес адаптера: {State.host}:{State.port or 35000}")
    else:
        print("  Адрес адаптера: стандартный (192.168.0.10:35000)")
    print()


def do_scan():
    clear()
    run(State.net_args() + ["scan"])
    pause()


def do_dtc():
    clear()
    run(State.net_args() + ["dtc"])
    pause()


def do_dtc_clear():
    clear()
    print("Ошибки будут прочитаны и СТЁРТЫ.")
    print()
    print("После этого: выключить и включить зажигание, включить заднюю")
    print("передачу на 30 секунд и прочитать снова пунктом 2.")
    print("Вернувшиеся ошибки актуальные, остальные были старыми следами.")
    print()
    if ask("Продолжить? (д/н): ").lower() not in ("д", "y", "да", "yes"):
        return
    run(State.net_args() + ["dtc", "--clear"])
    pause()


def do_info():
    clear()
    run(State.net_args() + ["info"])
    pause()


def do_dids():
    clear()
    print("Диапазон поиска идентификаторов данных.")
    print("По умолчанию 1000-10FF. Если ничего не найдётся, пробовать")
    print("другие: 2000-20FF, F400-F4FF, 0100-01FF")
    print()
    rng = ask("Диапазон (Enter — по умолчанию): ", "1000-10FF")
    clear()
    run(State.net_args() + ["dids", "--range", rng])
    pause()


def do_watch():
    clear()
    print("Список номеров через запятую, например 1001,1002,1003,1004")
    print("Взять их из результатов пункта 5.")
    print()
    dids = ask("Номера: ")
    if not dids:
        return
    print()
    print("Включите заднюю передачу и поводите рукой перед одним датчиком.")
    print("Меняющееся значение и есть этот датчик.")
    print("Остановить: Ctrl+C")
    pause()
    clear()
    run(State.net_args() + ["watch", "--dids", dids])
    pause()


def do_shake():
    clear()
    print("=" * 70)
    print("  ПРОВОКАЦИОННЫЙ ТЕСТ")
    print("=" * 70)
    print()
    print("  1. Включить зажигание и заднюю передачу")
    print("  2. Положить планшет на бампер")
    print("  3. Обеими руками медленно шевелить и подёргивать жгут")
    print("     по всей длине, особенно у разъёмов и в местах перегиба")
    print("  4. Запоминать, что именно вы трясёте в каждый момент")
    print("  5. Программа пискнет, когда контакт пропадёт")
    print()
    minutes = ask("Длительность в минутах (Enter — 10): ", "10")
    dids = ask("Номера датчиков через запятую (Enter — без них): ")
    argv = State.net_args() + ["shake", "--minutes", minutes]
    if dids:
        argv += ["--dids", dids]
    clear()
    run(argv)
    pause()


def do_full():
    clear()
    print("Полный обход всех блоков автомобиля со снятием ошибок.")
    print("Занимает три-пять минут. Нужен, чтобы увидеть общую картину")
    print("повреждений и найти блок парктроника среди безымянных.")
    print()
    pause()
    clear()
    run(State.net_args() + ["full"])
    pause()


def do_probe():
    clear()
    print("Детальный опрос блока: идентификация, ошибки, сырые байты.")
    print()

    modules = load_config().get("modules", [])
    if not modules:
        print("Список блоков пуст — сначала выполните пункт 1, поиск блоков.")
        print("После него блоки можно будет выбирать из списка,")
        print("не вводя адреса вручную.")
        pause()
        return

    print("  0  — опросить ВСЕ блоки подряд (рекомендуется)")
    print()
    for number, item in enumerate(modules, start=1):
        name = item.get("name") or "имя не читалось"
        print(f"  {number:<3}— 0x{int(item['id']):03X}   {name}")
    print()

    choice = ask("Номер из списка или 0 для обхода всех: ")
    if choice == "":
        return

    clear()
    if choice == "0":
        run(State.net_args() + ["probe", "--all"])
    else:
        try:
            item = modules[int(choice) - 1]
        except (ValueError, IndexError):
            print("[!] Такого номера в списке нет.")
            pause()
            return
        run(State.net_args() + ["--id", f"{int(item['id']):03X}", "probe"])
    pause()


def do_target():
    clear()
    print("Целевой опрос блока парктроника.")
    print()
    print("Обычный обход тратит на каждый блок десятки команд, а этот")
    print("адаптер после полусотни перестаёт отвечать. Здесь опрашивается")
    print("только нужный блок, и весь запас команд уходит на подбор")
    print("рабочего способа получить длинный ответ.")
    print()
    print("Адрес по умолчанию 70A. Enter — оставить его.")
    can_id = ask("Адрес блока: ", "70A")
    clear()
    run(State.net_args() + ["--id", can_id, "target"])
    pause()


def do_target_clear():
    clear()
    print("Стирание памяти ошибок парктроника и повторное чтение.")
    print()
    print("Зачем. Пока в памяти девять записей, ответ блока длинный")
    print("и обрывается на первом кадре — целиком его этот адаптер")
    print("прочитать не может. После стирания вернутся только те ошибки,")
    print("что возникают прямо сейчас. Их будет одна-две, ответ уместится")
    print("в один кадр, и коды прочитаются полностью.")
    print()
    print("Старые записи при этом теряются, но их количество и признаки")
    print("уже сняты, а для ремонта важны именно действующие неисправности.")
    print()
    if ask("Продолжить? (д/н): ").lower() not in ("д", "y", "да", "yes"):
        return
    can_id = ask("Адрес блока (Enter — 70A): ", "70A")
    clear()
    run(State.net_args() + ["--id", can_id, "target", "--clear"])
    pause()


def do_monitor():
    clear()
    print("Монитор числа активных ошибок.")
    print()
    print("Полный список ошибок этот адаптер прочитать не может, но их")
    print("ЧИСЛО берётся из заголовка ответа и читается всегда. Этого")
    print("хватает, чтобы найти неисправный канал перебором.")
    print()
    print("Порядок: отключить все датчики проверяемого бампера (их четыре")
    print("спереди и четыре сзади), запомнить число, а потом")
    print("подключать по одному. После какого датчика число не уменьшилось —")
    print("в том канале и неисправность.")
    print()
    print("Зажигание включено, задняя передача включена, двигатель заглушен.")
    print()
    can_id = ask("Адрес блока (Enter — 70A): ", "70A")
    clear()
    run(State.net_args() + ["--id", can_id, "monitor"])
    pause()


def do_monitor():
    clear()
    print("Живой счётчик ошибок для поиска неисправных каналов.")
    print()
    print("Порядок работы:")
    print("  1. Зажигание включено, двигатель заглушен, ЗАДНЯЯ ПЕРЕДАЧА")
    print("     включена — без неё блок датчики не опрашивает")
    print("  2. Отключить все датчики проверяемого бампера — их четыре")
    print("     спереди и четыре сзади, проверяем по одному бамперу за раз")
    print("  3. Запустить монитор, запомнить опорное число активных ошибок")
    print("  4. Подключать датчики по одному, после каждого ждать 10-15 секунд")
    print("  5. Стало меньше — канал исправен. Не изменилось — вот он, дефект")
    print()
    print("Программа пищит при каждом изменении счётчика.")
    print("Остановить: Ctrl+C")
    print()
    can_id = ask("Адрес блока (Enter — 70A): ", "70A")
    clear()
    run(State.net_args() + ["--id", can_id, "monitor"])
    pause()


def do_selftest():
    clear()
    print("=" * 70)
    print("  ПРОВЕРКА ДОМА, БЕЗ АВТОМОБИЛЯ")
    print("=" * 70)
    print()
    print("Запускается заглушка, изображающая блок парктроника.")
    print("Она специально имитирует плавающий контакт: канал 1003")
    print("пропадает каждые семь секунд.")
    print()
    print("Программа должна найти блок, прочитать ошибки и поймать")
    print("эти пропадания. Так проверяется работоспособность пакета")
    print("ещё до похода к машине.")
    print()
    pause()

    mock_path = os.path.join(BASE, "mock_elm327.py")
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [sys.executable, mock_path, "35003", "--glitch"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creation)
    time.sleep(2)

    local = ["--host", "127.0.0.1", "--port", "35003"]
    try:
        clear()
        print("--- Поиск блока ---\n")
        run(local + ["scan", "--quick"])
        print("\n--- Чтение ошибок ---\n")
        run(local + ["--id", "776", "dtc"])
        print("\n--- Провокационный тест, 30 секунд ---\n")
        run(local + ["--id", "776", "shake", "--minutes", "0.5",
                     "--dids", "1001,1003"])
        print()
        print("=" * 70)
        print("Если выше были найдены блок, две ошибки и события пропадания")
        print("канала 1003 — пакет полностью работоспособен.")
        print("=" * 70)
    finally:
        process.terminate()
        try:
            os.remove(os.path.join(os.getcwd(), "pdc_config.json"))
        except OSError:
            pass
    pause()


def do_sethost():
    clear()
    print("Адрес адаптера по умолчанию: 192.168.0.10, порт 35000")
    print()
    print("Посмотреть свой: Параметры — Сеть — Wi-Fi — Свойства оборудования,")
    print("строка «Шлюз по умолчанию».")
    print("Другие частые варианты: 192.168.4.1 и 192.168.1.10")
    print()
    host = ask("Адрес (Enter — сбросить на стандартный): ")
    if not host:
        State.host = None
        State.port = None
        return
    port = ask("Порт (Enter — 35000): ", "35000")
    State.host = host
    try:
        State.port = int(port)
    except ValueError:
        State.port = 35000


ACTIONS = {
    "1": do_scan,
    "2": do_dtc,
    "3": do_dtc_clear,
    "4": do_info,
    "5": do_dids,
    "6": do_watch,
    "7": do_shake,
    "8": do_selftest,
    "9": do_sethost,
    "10": do_full,
    "11": do_probe,
    "12": do_target,
    "13": do_target_clear,
    "14": do_monitor,
}


def main():
    setup_console()
    while True:
        show_menu()
        choice = ask("Введите номер и нажмите Enter: ")
        if choice == "0":
            return 0
        action = ACTIONS.get(choice)
        if action:
            action()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
