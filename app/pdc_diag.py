#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdc_diag.py — диагностика парковочной системы VW Polo Sedan через ELM327 Wi-Fi.
Версия 1.0. Требует файл pdc_core.py в той же папке.

Режимы: scan, info, dtc, dids, watch, shake.
Подробности: python pdc_diag.py --help
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

from pdc_core import (DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TX_ID,
                      PARKING_AID_ADDR, CsvLog, Elm327, UdsClient, UdsError,
                      assemble_iso_tp, beep, describe_status, format_dtc,
                      VERSION, build_bundle, clear_old_reports, declared_length,
                      dump_raw_log, failure_text, hint_for_dtc, is_negative,
                      is_truncated, partial_payload, timestamp)

CONFIG_PATH = "pdc_config.json"

# Идентификаторы общей информации о блоке (стандарт UDS)
INFO_DIDS = {
    0xF187: "номер запчасти VAG",
    0xF189: "версия программного обеспечения",
    0xF18A: "код изготовителя блока",
    0xF190: "VIN автомобиля",
    0xF191: "аппаратный номер блока",
    0xF197: "название блока",
}


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {}


def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def resolve_tx_id(args):
    """Определяет идентификатор блока: из ключа, из конфигурации или по умолчанию."""
    if args.id:
        return int(args.id, 16)
    config = load_config()
    if "tx_id" in config:
        return int(config["tx_id"])
    return DEFAULT_TX_ID


# ---------------------------------------------------------------------------
# Подключение
# ---------------------------------------------------------------------------
def open_link(args, announce=True):
    """Открывает связь с адаптером и приводит его в рабочее состояние."""
    elm = Elm327(args.host, args.port, verbose=args.verbose)
    try:
        elm.connect()
    except OSError as exc:
        print(f"[!] Не удалось подключиться к адаптеру {args.host}:{args.port}")
        print(f"    {exc}")
        print("    Проверь, что компьютер в сети Wi-Fi адаптера.")
        return None

    elm.init()
    if announce:
        version, voltage = elm.identify()
        print(f"  Адаптер    : {version}")
        print(f"  Напряжение : {voltage}")
        try:
            value = float(voltage.upper().replace("V", "").strip())
            if value > 13.3:
                print("  [!] Напряжение выше 13,3 В — похоже, двигатель запущен.")
                print("      Для диагностики его лучше заглушить,")
                print("      оставив включённым зажигание.")
            if value < 11.5:
                print("  [!] Напряжение ниже 11,5 В. Блоки могут выдавать ложные")
                print("      ошибки. Зарядить аккумулятор перед диагностикой.")
        except ValueError:
            pass
    return elm


def check_bus(elm):
    """
    Проверяет живость шины стандартным запросом к двигателю.

    Одна неудачная попытка ничего не доказывает: дешёвый адаптер часто
    не отвечает на первый запрос после подключения. Поэтому пробуем
    несколько раз, а при неудаче — переключаем протокол на автоопределение.
    И даже полный отказ не останавливает работу: опрос блоков может
    пройти успешно, а проверка шины здесь лишь справочная.
    """
    for attempt in range(3):
        elm.set_header(0x7E0)
        answer = elm.cmd("0100", read_timeout=3.0)
        if not is_negative(answer):
            print("  Шина       : отвечает")
            return True
        time.sleep(0.5)

    print("  Шина       : на стандартный запрос не ответила")
    print("      Пробую автоопределение протокола...")
    elm.cmd("ATSP0")
    elm.current_header = None
    for attempt in range(2):
        elm.set_header(0x7E0)
        answer = elm.cmd("0100", read_timeout=4.0)
        if not is_negative(answer):
            print("  Шина       : отвечает после автоопределения")
            return True
        time.sleep(0.5)

    elm.cmd("ATSP6")
    elm.current_header = None
    print()
    print("  [!] Стандартный запрос к двигателю остался без ответа.")
    print("      Возможные причины по убыванию вероятности:")
    print("        1. Адаптер завис — вынуть из разъёма на 10 секунд,")
    print("           вставить обратно, заново подключиться к его Wi-Fi.")
    print("        2. Выключено зажигание.")
    print("        3. Двигатель запущен — заглушить, оставить зажигание.")
    print()
    print("      Продолжаю работу: опрос блоков может пройти и без этого.")
    return True


# ---------------------------------------------------------------------------
# Режим scan
# ---------------------------------------------------------------------------
def mode_scan(args):
    print("=" * 70)
    print(f"РЕЖИМ SCAN — поиск блоков на шине      (версия программы {VERSION})")
    print("=" * 70)
    removed = clear_old_reports()
    if removed:
        print(f"  Удалено отчётов прошлых запусков: {len(removed)}")

    elm = open_link(args)
    if not elm:
        return 1
    try:
        if not check_bus(elm):
            return 1

        elm.accept_all()
        ids = list(range(0x700, 0x800)) if not args.quick else [
            DEFAULT_TX_ID, 0x7E0, 0x710, 0x713, 0x714, 0x715, 0x70A, 0x76E]

        print(f"\n  Проверяется идентификаторов: {len(ids)}")
        print("  Запрос 3E 00 (TesterPresent) — только чтение.\n")

        log = CsvLog("scan_result.csv", ["время", "can_id", "ответ", "вывод"])
        found = []
        for index, can_id in enumerate(ids, start=1):
            elm.set_header(can_id)
            lines = elm.cmd("3E00", read_timeout=0.6)
            if not is_negative(lines):
                source, data = assemble_iso_tp(lines)
                if data:
                    found.append(can_id)
                    mark = "  <-- парктроник" if can_id == DEFAULT_TX_ID else ""
                    print(f"  [ОТВЕТ] 0x{can_id:03X} -> {data.hex().upper()}{mark}")
                    log.add(время=timestamp(), can_id=f"0x{can_id:03X}",
                            ответ=data.hex().upper(), вывод="блок отозвался")
            if index % 32 == 0:
                print(f"  ... проверено {index} из {len(ids)}")

        log.close()
        print(f"\n  Отозвалось блоков: {len(found)}")

        # Опознание найденных блоков по имени и номеру запчасти.
        # Нужно, чтобы понять, нет ли парктроника под неожиданным адресом.
        named = {}
        if found:
            print("\n  Опознание блоков...\n")
            name_log = CsvLog("modules_result.csv",
                              ["can_id", "название", "номер_запчасти"])
            for can_id in found:
                probe = UdsClient(elm, can_id)
                name, part = "", ""
                for did, target in ((0xF197, "name"), (0xF187, "part")):
                    try:
                        raw = probe.read_did(did, read_timeout=1.0)
                    except (UdsError, OSError):
                        continue
                    text = "".join(
                        chr(b) if 32 <= b < 127 else "" for b in raw).strip()
                    if target == "name":
                        name = text
                    else:
                        part = text
                named[can_id] = name
                label = name or "имя не читается"
                print(f"  0x{can_id:03X}  {label:<32} {part}")
                name_log.add(can_id=f"0x{can_id:03X}", название=name,
                             номер_запчасти=part)
            name_log.close()

            # Сохраняем список найденных блоков, чтобы дальше выбирать
            # их из меню, а не вводить адреса руками
            config = load_config()
            config["modules"] = [{"id": can_id, "name": named.get(can_id, "")}
                                 for can_id in found]
            config["host"] = args.host
            config["port"] = args.port
            save_config(config)
            print(f"\n  Список блоков сохранён в {CONFIG_PATH}")
            dump_raw_log(elm)
            build_bundle(elm)
            print("  Полный обмен с адаптером: elm_raw.log")
            print("  Файл для отправки: REPORT_TO_SEND.txt")

        # Ищем среди опознанных что-то похожее на парктроник
        keywords = ("PAR", "PDC", "EPH", "ASSIST", "AREA", "UMFELD",
                    "EINPARK", "OPS")
        suspects = [can_id for can_id, name in named.items()
                    if can_id != 0x700
                    and any(word in name.upper() for word in keywords)]

        # Проба протокола TP 2.0 на случай, если UDS промолчал
        tp_id = 0x200 + PARKING_AID_ADDR
        elm.cmd("ATCAF0")
        elm.set_header(tp_id)
        tp_answer = elm.cmd("00000340000301", read_timeout=1.5)
        elm.cmd("ATCAF1")
        tp_alive = not is_negative(tp_answer)
        print(f"  Проба TP 2.0 на 0x{tp_id:03X}: "
              f"{'есть ответ' if tp_alive else 'ответа нет'}")

        print("\n" + "=" * 70)
        if DEFAULT_TX_ID in found:
            print(f"Блок парктроника найден на 0x{DEFAULT_TX_ID:03X}, протокол UDS.")
            print("Программа готова к работе. Дальше: пункт 2 меню.")
            config = load_config()
            config.update({"tx_id": DEFAULT_TX_ID, "host": args.host,
                           "port": args.port})
            save_config(config)
            print(f"Идентификатор сохранён в {CONFIG_PATH}")
        elif suspects:
            chosen = suspects[0]
            print(f"Парктроник найден под нестандартным адресом 0x{chosen:03X}")
            print(f"по имени блока: {named[chosen]}")
            config = load_config()
            config.update({"tx_id": chosen, "host": args.host, "port": args.port})
            save_config(config)
            print(f"Идентификатор сохранён в {CONFIG_PATH}. Дальше: пункт 2 меню.")
        elif found:
            print("ВАЖНЫЙ РЕЗУЛЬТАТ: шина и адаптер исправны, блоки отвечают,")
            print("но блока парктроника среди них нет.")
            print()
            print("Что это означает. Опрошен весь диапазон адресов, шлюз")
            print("маршрутизирует запросы нормально — иначе не ответил бы никто.")
            print("Молчащий блок при живой шине означает одно из двух:")
            print("  1. блок обесточен — нет питания или потеряна масса")
            print("  2. блок вышел из строя — типично после залива водой")
            print()
            print("В обоих случаях это и есть искомая неисправность, и она")
            print("объясняет отказ сразу всех восьми датчиков и камеры.")
            print("Датчики здесь ни при чём, прозванивать их бессмысленно.")
            print()
            print("Дальше работаем мультиметром на разъёме самого блока:")
            print("  - найти блок за обшивкой багажника")
            print("  - проверить питание +12 В и массу на его разъёме")
            print("  - осмотреть блок и разъём на следы воды и коррозии")
            print("  - проверить предохранитель парктроника")
            print()
            print("Список опознанных блоков сохранён в modules_result.csv —")
            print("пришлите его, разберём, не прячется ли парктроник под")
            print("нестандартным именем.")
        elif tp_alive:
            print("UDS молчит, но TP 2.0 отвечает.")
            print("Эта программа работает по UDS и с TP 2.0 не справится:")
            print("протокол требует удержания канала по таймеру, а Wi-Fi адаптер")
            print("даёт плавающую задержку. Здесь нужен кабель VCDS.")
        else:
            print("Ни один блок не ответил при живой шине.")
            print("Вероятно, адаптер не пропускает кадры к блокам кузовной")
            print("электроники — частая беда дешёвых клонов ELM327.")
        print("Отчёт: scan_result.csv")
        return 0
    finally:
        elm.close()


# ---------------------------------------------------------------------------
# Режим info
# ---------------------------------------------------------------------------
def mode_info(args):
    tx_id = resolve_tx_id(args)
    print("=" * 70)
    print(f"РЕЖИМ INFO — идентификация блока 0x{tx_id:03X}")
    print("=" * 70)

    elm = open_link(args)
    if not elm:
        return 1
    try:
        elm.accept_all()
        uds = UdsClient(elm, tx_id)

        if not uds.tester_present():
            print("\n[!] Блок не отвечает. Сначала выполнить: python pdc_diag.py scan")
            return 1
        print("\n  Блок на связи.\n")

        log = CsvLog("info_result.csv", ["идентификатор", "название",
                                         "значение", "байты"])
        for did, title in INFO_DIDS.items():
            try:
                value = uds.read_did(did)
            except UdsError as exc:
                print(f"  {did:04X}  {title:<34} — {exc}")
                continue
            text = value.decode("ascii", errors="replace").strip()
            printable = "".join(ch if 32 <= ord(ch) < 127 else "." for ch in text)
            print(f"  {did:04X}  {title:<34} {printable}")
            log.add(идентификатор=f"{did:04X}", название=title,
                    значение=printable, байты=value.hex().upper())
        log.close()
        print("\nОтчёт: info_result.csv")
        return 0
    finally:
        elm.close()


# ---------------------------------------------------------------------------
# Режим dtc
# ---------------------------------------------------------------------------
def mode_dtc(args):
    tx_id = resolve_tx_id(args)
    print("=" * 70)
    print(f"РЕЖИМ DTC — ошибки блока 0x{tx_id:03X}")
    print("=" * 70)

    elm = open_link(args)
    if not elm:
        return 1
    try:
        elm.accept_all()
        uds = UdsClient(elm, tx_id)

        if not uds.tester_present():
            print("\n[!] Блок не отвечает. Сначала выполнить: python pdc_diag.py scan")
            return 1

        # Блоки кузовной электроники отдают ошибки только в расширенной сессии
        uds.start_session(0x03)

        try:
            faults = uds.read_dtcs()
        except UdsError as exc:
            print(f"\n[!] Не удалось прочитать ошибки: {exc}")
            return 1

        if not faults:
            print("\n  Память ошибок пуста.")
            print("  Если система при этом не работает — блок исправен, но до него")
            print("  не доходит команда включения. Смотреть цепь заднего хода,")
            print("  кнопку парктроника и предохранитель.")
            return 0

        print(f"\n  Найдено ошибок: {len(faults)}\n")
        log = CsvLog("dtc_result.csv", ["время", "код", "байты", "тип_отказа",
                                        "статус", "признаки", "трактовка"])
        for code_bytes, status in faults:
            text = format_dtc(code_bytes)
            failure = failure_text(code_bytes)
            marks = describe_status(status)
            hint = hint_for_dtc(text, status)
            print(f"  {text}   статус 0x{status:02X}")
            print(f"      тип отказа: {failure}")
            print(f"      признаки  : {marks}")
            print(f"      трактовка : {hint}\n")
            log.add(время=timestamp(), код=text, байты=code_bytes.hex().upper(),
                    тип_отказа=failure, статус=f"0x{status:02X}",
                    признаки=marks, трактовка=hint)
        log.close()

        print("  Номер ошибки указывает на канал, тип отказа — на характер дефекта.")
        print("  «Обрыв цепи» ищется прозвонкой, «замыкание на массу» — проверкой")
        print("  изоляции, «нет сообщений от узла» — питанием датчика.")
        print("  Состояние важнее всего: «активна сейчас» ищется замерами,")
        print("  «была ранее» — шевелением жгута в режиме shake.")
        print("\nОтчёт: dtc_result.csv")

        if args.clear:
            print("\n  Стирание памяти ошибок...")
            try:
                uds.clear_dtcs()
                print("  Готово. Теперь: выключить и включить зажигание,")
                print("  включить заднюю передачу на 30 секунд и прочитать снова.")
                print("  Вернувшиеся ошибки — актуальные, остальные были старыми.")
            except UdsError as exc:
                print(f"  [!] Не удалось стереть: {exc}")
        return 0
    finally:
        elm.close()


# ---------------------------------------------------------------------------
# Режим dids
# ---------------------------------------------------------------------------
def mode_dids(args):
    tx_id = resolve_tx_id(args)
    try:
        start_text, end_text = args.range.split("-")
        start, end = int(start_text, 16), int(end_text, 16)
    except ValueError:
        print("[!] Диапазон задаётся так: --range 1000-10FF")
        return 1

    print("=" * 70)
    print(f"РЕЖИМ DIDS — поиск живых данных блока 0x{tx_id:03X}")
    print("=" * 70)
    print(f"  Диапазон: {start:04X}-{end:04X}  ({end - start + 1} шт.)")
    print("  Блок отвечает не на все идентификаторы — это нормально.")
    print("  Задача: найти те, что вообще существуют.\n")

    elm = open_link(args)
    if not elm:
        return 1
    try:
        elm.accept_all()
        uds = UdsClient(elm, tx_id)
        if not uds.tester_present():
            print("[!] Блок не отвечает. Сначала: python pdc_diag.py scan")
            return 1

        log = CsvLog("dids_result.csv", ["идентификатор", "длина", "байты",
                                         "как_число"])
        found = 0
        for index, did in enumerate(range(start, end + 1), start=1):
            try:
                value = uds.read_did(did, read_timeout=0.7)
            except UdsError:
                continue
            except OSError:
                print("  [!] Связь потеряна.")
                break
            found += 1
            as_number = int.from_bytes(value[:2], "big") if value else 0
            print(f"  [{did:04X}]  {len(value)} байт  "
                  f"{value.hex().upper():<20} ~{as_number}")
            log.add(идентификатор=f"{did:04X}", длина=len(value),
                    байты=value.hex().upper(), как_число=as_number)
            if index % 64 == 0:
                print(f"  ... проверено {index}")
        log.close()

        print(f"\n  Найдено доступных идентификаторов: {found}")
        print("\n  Как понять, какие из них — датчики расстояния:")
        print("  1. Запустить: python pdc_diag.py watch --dids СПИСОК")
        print("  2. Включить заднюю передачу")
        print("  3. Поводить рукой перед одним датчиком")
        print("  4. Меняющееся значение и есть этот датчик")
        print("\nОтчёт: dids_result.csv")
        return 0
    finally:
        elm.close()


# ---------------------------------------------------------------------------
# Режим watch
# ---------------------------------------------------------------------------
def mode_watch(args):
    tx_id = resolve_tx_id(args)
    try:
        dids = [int(item.strip(), 16) for item in args.dids.split(",") if item.strip()]
    except ValueError:
        print("[!] Список задаётся так: --dids 1001,1002,1003")
        return 1
    if not dids:
        print("[!] Не указано ни одного идентификатора.")
        return 1

    print("=" * 70)
    print(f"РЕЖИМ WATCH — живые данные блока 0x{tx_id:03X}")
    print("=" * 70)
    print(f"  Отслеживается идентификаторов: {len(dids)}")
    print("  Прервать: Ctrl+C\n")

    elm = open_link(args)
    if not elm:
        return 1
    try:
        elm.accept_all()
        uds = UdsClient(elm, tx_id)
        if not uds.tester_present():
            print("[!] Блок не отвечает. Сначала: python pdc_diag.py scan")
            return 1

        columns = ["время", "секунда"] + [f"{did:04X}" for did in dids]
        log = CsvLog("watch_log.csv", columns)
        started = time.time()

        print("  " + "  ".join(f"{did:04X}".rjust(8) for did in dids))
        print("  " + "-" * (10 * len(dids)))

        try:
            while True:
                row = {"время": timestamp(),
                       "секунда": f"{time.time() - started:.2f}"}
                cells = []
                for did in dids:
                    try:
                        value = uds.read_did(did, read_timeout=0.7)
                        as_number = int.from_bytes(value[:2], "big") if value else 0
                        cells.append(f"{as_number}".rjust(8))
                        row[f"{did:04X}"] = as_number
                    except UdsError:
                        cells.append("нет".rjust(8))
                        row[f"{did:04X}"] = ""
                print("  " + "  ".join(cells))
                log.add(**row)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  [остановлено]")
        finally:
            log.close()
        print("\nЖурнал: watch_log.csv")
        return 0
    finally:
        elm.close()


# ---------------------------------------------------------------------------
# Режим shake — провокационный тест
# ---------------------------------------------------------------------------
def mode_shake(args):
    tx_id = resolve_tx_id(args)

    print("=" * 70)
    print("РЕЖИМ SHAKE — провокационный тест")
    print("=" * 70)
    print("""
  Что делать физически:
    1. Включить зажигание и заднюю передачу, автомобиль стоит
    2. Запустить этот режим и положить ноутбук на бампер
    3. Обеими руками медленно шевелить, покачивать и подёргивать жгут
       по всей длине — особенно у разъёмов и в местах перегиба
    4. Проговаривать вслух или запоминать, что именно вы трясёте
    5. Программа пискнет в момент, когда контакт пропадёт

  Программа опрашивает блок в цикле и фиксирует три вида событий:
    - потеря связи с блоком
    - появление новой ошибки
    - изменение состояния существующей ошибки

  Прервать: Ctrl+C
""")

    elm = open_link(args)
    if not elm:
        return 1
    try:
        elm.accept_all()
        uds = UdsClient(elm, tx_id)
        if not uds.tester_present():
            print("[!] Блок не отвечает. Сначала: python pdc_diag.py scan")
            return 1

        watched = []
        if args.dids:
            try:
                watched = [int(item.strip(), 16)
                           for item in args.dids.split(",") if item.strip()]
            except ValueError:
                print("[!] Неверный список идентификаторов, продолжаю без них.")

        log = CsvLog("shake_log.csv",
                     ["время", "секунда", "событие", "подробности"])
        started = time.time()
        deadline = started + args.minutes * 60

        previous_faults = {}
        try:
            faults = uds.read_dtcs()
            previous_faults = {format_dtc(code): status for code, status in faults}
        except UdsError:
            pass

        if previous_faults:
            print(f"  Исходное состояние: ошибок {len(previous_faults)}")
            for text, status in previous_faults.items():
                print(f"    {text}  ({describe_status(status)})")
        else:
            print("  Исходное состояние: память ошибок пуста")

        print(f"\n  Тест идёт {args.minutes} мин. Начали. Шевелите жгут.\n")

        events = 0
        link_ok = True
        last_dtc_check = 0.0
        previous_values = {}

        while time.time() < deadline:
            now = time.time()
            elapsed = now - started

            # Быстрая проверка связи
            alive = uds.tester_present()
            if alive != link_ok:
                link_ok = alive
                events += 1
                event = "СВЯЗЬ ВОССТАНОВЛЕНА" if alive else "СВЯЗЬ ПОТЕРЯНА"
                print(f"  [{elapsed:7.2f} с]  {event}")
                beep("alert" if not alive else "ok")
                log.add(время=timestamp(), секунда=f"{elapsed:.2f}",
                        событие=event, подробности="опрос блока")

            # Наблюдаемые данные, если заданы
            if watched and link_ok:
                for did in watched:
                    try:
                        value = uds.read_did(did, read_timeout=0.5)
                        as_number = int.from_bytes(value[:2], "big") if value else 0
                        state = str(as_number)
                    except UdsError:
                        state = "нет ответа"
                    if previous_values.get(did) == "нет ответа" and state != "нет ответа":
                        events += 1
                        print(f"  [{elapsed:7.2f} с]  канал {did:04X} вернулся")
                        log.add(время=timestamp(), секунда=f"{elapsed:.2f}",
                                событие="канал вернулся", подробности=f"{did:04X}")
                    elif previous_values.get(did) not in (None, "нет ответа") \
                            and state == "нет ответа":
                        events += 1
                        print(f"  [{elapsed:7.2f} с]  канал {did:04X} ПРОПАЛ")
                        beep("alert")
                        log.add(время=timestamp(), секунда=f"{elapsed:.2f}",
                                событие="канал пропал", подробности=f"{did:04X}")
                    previous_values[did] = state

            # Ошибки — реже, чтение занимает больше времени
            if link_ok and now - last_dtc_check >= args.dtc_interval:
                last_dtc_check = now
                try:
                    faults = uds.read_dtcs()
                    current = {format_dtc(code): status for code, status in faults}
                except UdsError:
                    current = previous_faults

                for text, status in current.items():
                    if text not in previous_faults:
                        events += 1
                        print(f"  [{elapsed:7.2f} с]  НОВАЯ ОШИБКА {text}"
                              f"  ({describe_status(status)})")
                        beep("alert")
                        log.add(время=timestamp(), секунда=f"{elapsed:.2f}",
                                событие="новая ошибка",
                                подробности=f"{text} / {describe_status(status)}")
                    elif previous_faults[text] != status:
                        events += 1
                        print(f"  [{elapsed:7.2f} с]  изменилось состояние {text}")
                        log.add(время=timestamp(), секунда=f"{elapsed:.2f}",
                                событие="изменение состояния",
                                подробности=f"{text} / {describe_status(status)}")
                previous_faults = current

            time.sleep(args.interval)

        print(f"\n  Время вышло. Событий зафиксировано: {events}")

    except KeyboardInterrupt:
        print("\n  [остановлено вручную]")
    finally:
        try:
            log.close()
        except Exception:
            pass
        elm.close()

    print("\n" + "=" * 70)
    print("Журнал: shake_log.csv")
    print("Сопоставьте метки времени с тем, что вы трясли в эти секунды.")
    print("Место, где событие повторяется стабильно, и есть неисправность.")
    print("=" * 70)
    return 0


def mode_full(args):
    """
    Полный обход: находит все блоки и снимает с каждого ошибки.
    Нужен, когда неизвестно, какой блок отвечает за нужную систему,
    и чтобы увидеть общую картину повреждений после залива.
    """
    print("=" * 70)
    print("РЕЖИМ FULL — полный отчёт по всем блокам")
    print("=" * 70)
    print("  Опрашиваются все блоки, с каждого снимаются ошибки.")
    print("  Занимает три-пять минут.\n")

    elm = open_link(args)
    if not elm:
        return 1
    try:
        if not check_bus(elm):
            return 1
        elm.accept_all()

        ids = list(range(0x700, 0x800))
        print(f"  Поиск блоков среди {len(ids)} адресов...\n")
        found = []
        for index, can_id in enumerate(ids, start=1):
            elm.set_header(can_id)
            lines = elm.cmd("3E00", read_timeout=0.6)
            if not is_negative(lines):
                _, data = assemble_iso_tp(lines)
                if data:
                    found.append(can_id)
            if index % 64 == 0:
                print(f"  ... проверено {index} из {len(ids)}")

        print(f"\n  Найдено блоков: {len(found)}")
        print("  Снятие ошибок с каждого...\n")

        log = CsvLog("full_report.csv",
                     ["can_id", "название", "номер_запчасти", "всего_ошибок",
                      "код", "тип_отказа", "статус", "признаки"])

        total_faults = 0
        for can_id in found:
            probe = UdsClient(elm, can_id)

            name, part = "", ""
            for did, slot in ((0xF197, "name"), (0xF187, "part")):
                try:
                    raw = probe.read_did(did, read_timeout=1.0)
                except (UdsError, OSError):
                    continue
                text = "".join(chr(b) if 32 <= b < 127 else "" for b in raw).strip()
                if slot == "name":
                    name = text
                else:
                    part = text

            probe.start_session(0x03)
            try:
                faults = probe.read_dtcs()
            except (UdsError, OSError):
                try:
                    faults = probe.read_dtcs_kwp()
                except (UdsError, OSError) as exc2:
                    faults = None
                    exc = exc2
            if faults is None:
                print(f"  0x{can_id:03X}  {name or '—':<24} ошибки не читаются ({exc})")
                log.add(can_id=f"0x{can_id:03X}", название=name,
                        номер_запчасти=part, всего_ошибок="нет доступа",
                        код="", тип_отказа="", статус="", признаки="")
                continue

            total_faults += len(faults)
            marker = "  <<< ЕСТЬ ОШИБКИ" if faults else ""
            print(f"  0x{can_id:03X}  {name or '—':<24} ошибок: {len(faults)}{marker}")

            if not faults:
                log.add(can_id=f"0x{can_id:03X}", название=name,
                        номер_запчасти=part, всего_ошибок=0,
                        код="", тип_отказа="", статус="", признаки="")
            for code_bytes, status in faults:
                text = format_dtc(code_bytes)
                failure = failure_text(code_bytes)
                marks = describe_status(status)
                print(f"           {text}  {failure}  [{marks}]")
                log.add(can_id=f"0x{can_id:03X}", название=name,
                        номер_запчасти=part, всего_ошибок=len(faults),
                        код=text, тип_отказа=failure,
                        статус=f"0x{status:02X}", признаки=marks)
        log.close()

        print("\n" + "=" * 70)
        print(f"Блоков опрошено: {len(found)}.  Всего ошибок: {total_faults}.")
        print()
        print("Как читать отчёт:")
        print("  - блок с ошибками вида «обрыв цепи» или «нет сообщений»")
        print("    указывает на повреждённую проводку в своей зоне;")
        print("  - если ошибки сгруппировались в блоках задней части кузова,")
        print("    это подтверждает версию залива;")
        print("  - блок парктроника может скрываться среди безымянных —")
        print("    ищите тот, где ошибки касаются датчиков расстояния.")
        print()
        print("Отчёт: full_report.csv")
        return 0
    finally:
        elm.close()


def probe_one(elm, tx_id, log, verbose=True):
    """
    Опрашивает один блок.

    Режим адаптера здесь намеренно не переустанавливается. Проверено
    на машине: при плотном потоке служебных команд дешёвый адаптер
    перестаёт отвечать вовсе, и все блоки выглядят молчащими. Режим
    поиска блоков, где на каждый адрес идёт всего одна команда,
    отрабатывает без сбоев — здесь делаем так же.
    """
    uds = UdsClient(elm, tx_id)
    result = {"id": tx_id, "alive": False, "name": "", "part": "",
              "faults": [], "note": "", "rx": None, "mf": None}

    if not uds.tester_present():
        # Пустая строка от адаптера и ответ «нет данных» — разные вещи.
        # Первое означает, что захлебнулся адаптер, второе — что молчит блок.
        last = elm.raw_log[-1][1] if elm.raw_log else ""
        if last.strip() == "":
            result["note"] = "адаптер не ответил"
            mark = "АДАПТЕР НЕ ОТВЕТИЛ"
        else:
            result["note"] = "не отвечает"
            mark = "БЛОК НЕ ОТВЕТИЛ"
        if verbose:
            print(f"\n  0x{tx_id:03X}: {result['note']}")
        log.add(блок=f"0x{tx_id:03X}", запрос="3E00",
                ответ_сырой=repr(last)[:60], расшифровка=mark)
        return result

    result["alive"] = True
    result["rx"] = uds.rx_id
    uds.start_session(0x03)

    for did, slot in ((0xF197, "name"), (0xF187, "part")):
        try:
            value = uds.read_did(did, read_timeout=1.5)
        except (UdsError, OSError) as exc:
            # Неудачу тоже записываем: пустая строка в отчёте не даёт
            # отличить «не спрашивали» от «спросили и не получили»
            log.add(блок=f"0x{tx_id:03X}", запрос=f"22{did:04X}",
                    ответ_сырой="", расшифровка=f"НЕ ПРОЧИТАНО: {exc}")
            continue
        text = "".join(chr(b) if 32 <= b < 127 else "" for b in value).strip()
        result[slot] = text
        log.add(блок=f"0x{tx_id:03X}", запрос=f"22{did:04X}",
                ответ_сырой=value.hex().upper(), расшифровка=text)

    try:
        raw = uds.read_dtcs_raw()
        log.add(блок=f"0x{tx_id:03X}", запрос="1902FF",
                ответ_сырой=raw.hex().upper(), расшифровка="сырые байты")
        records = raw[3:]
        if len(records) % 4:
            result["note"] = "длина не делится на 4 — разбор под вопросом"
        for offset in range(0, len(records) - 3, 4):
            code = records[offset:offset + 3]
            status = records[offset + 3]
            result["faults"].append((code, status))
    except (UdsError, OSError) as exc:
        result["note"] = str(exc)
        log.add(блок=f"0x{tx_id:03X}", запрос="1902FF",
                ответ_сырой="", расшифровка=f"НЕ ПРОЧИТАНО: {exc}")

    result["mf"] = uds.mf_strategy
    if verbose:
        label = result["name"] or "имя не читается"
        rx = f"ответ с 0x{result['rx']:03X}" if result["rx"] else ""
        print(f"\n  0x{tx_id:03X}  {label}   {result['part']}   {rx}")
        if result["faults"]:
            for code, status in result["faults"]:
                print(f"      {format_dtc(code)}  {failure_text(code)}"
                      f"  [{describe_status(status)}]")
        else:
            print(f"      ошибок нет"
                  f"{'  (' + result['note'] + ')' if result['note'] else ''}")
    return result


TARGET_DEFAULT = 0x70A


def read_fault_state(elm, mask):
    """
    Возвращает (количество, первый код, тип отказа) по указанному признаку.

    Количество берётся из заголовка ответа, поэтому работает даже когда
    список длинный и обрывается на первом кадре.
    """
    answer = elm.cmd(f"1902{mask:02X}", read_timeout=2.0)
    if is_negative(answer):
        return None, "", ""
    length = declared_length(answer)
    data = partial_payload(answer)
    if not data or data[0] != 0x59 or length is None or length < 3:
        return None, "", ""
    total = (length - 3) // 4
    records = data[3:]
    if len(records) >= 3:
        code = records[0:3]
        return total, format_dtc(code), failure_text(code)
    return total, "", ""


def mode_monitor(args):
    """
    Живой счётчик ошибок для поиска неисправных каналов отключением датчиков.

    Опрашивается только число ошибок — оно берётся из заголовка ответа
    и не требует чтения всего списка, который этот адаптер осилить не может.
    """
    tx_id = int(args.id, 16) if args.id else TARGET_DEFAULT

    print("=" * 70)
    print(f"МОНИТОР ОШИБОК БЛОКА 0x{tx_id:03X}"
          f"      (версия программы {VERSION})")
    print("=" * 70)
    print("""
  КАК ПОЛЬЗОВАТЬСЯ

    1. Зажигание включено, двигатель заглушен, задняя передача включена.
       Без задней передачи блок датчики не опрашивает и ошибки не обновит.

    2. Отключить все четыре задних датчика. Счётчик активных должен
       вырасти — это опорное значение, запомните его.

    3. Подключать датчики по одному. После каждого ждать 10-15 секунд,
       пока блок переопросит цепи.

    4. Смотреть на счётчик активных ошибок:
         стало меньше  -> подключённый датчик и его провод исправны
         не изменилось -> с этим каналом проблема, он и есть искомый

    5. Программа пищит при каждом изменении счётчика.

  Остановить: Ctrl+C
""")

    elm = open_link(args)
    if not elm:
        return 1

    log = CsvLog("monitor_log.csv",
                 ["время", "секунда", "активных", "всего", "первый_код",
                  "тип_отказа", "событие"])
    started = time.time()

    try:
        elm.accept_all()
        elm.set_header(tx_id)
        lines = elm.cmd("3E00", read_timeout=1.5)
        if is_negative(lines):
            print("  [!] Блок не отвечает. Проверить зажигание и адаптер.")
            return 1
        print("  Блок на связи. Начинаю опрос.\n")

        print("   время      активных   всего   первый активный код")
        print("   " + "-" * 60)

        previous_active = None
        try:
            while True:
                active, code, failure = read_fault_state(elm, 0x01)
                time.sleep(1.2)
                total, _, _ = read_fault_state(elm, 0xFF)

                elapsed = time.time() - started
                event = ""
                if active is not None and previous_active is not None \
                        and active != previous_active:
                    delta = active - previous_active
                    event = f"изменилось на {delta:+d}"
                    beep("alert")
                if active is not None:
                    previous_active = active

                shown_active = "—" if active is None else str(active)
                shown_total = "—" if total is None else str(total)
                line = (f"   {timestamp():<11}{shown_active:^10}{shown_total:^8}"
                        f" {code} {failure}")
                if event:
                    line += f"   <<< {event}"
                print(line)

                log.add(время=timestamp(), секунда=f"{elapsed:.1f}",
                        активных=shown_active, всего=shown_total,
                        первый_код=code, тип_отказа=failure, событие=event)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  [остановлено]")
    finally:
        log.close()
        build_bundle(elm)
        elm.close()

    print("\n  Журнал: monitor_log.csv")
    print("  Файл для отправки: REPORT_TO_SEND.txt")
    return 0


def mode_target(args):
    """
    Целевой опрос одного блока: минимум команд, набор способов передачи.

    Обычный обход тратит на каждый блок десятки служебных команд, а этот
    адаптер после полусотни перестаёт отвечать вовсе. Здесь опрашивается
    единственный нужный блок, и весь запас команд уходит на подбор
    рабочего способа получить длинный ответ.
    """
    tx_id = int(args.id, 16) if args.id else TARGET_DEFAULT

    print("=" * 70)
    print(f"ЦЕЛЕВОЙ ОПРОС БЛОКА 0x{tx_id:03X}"
          f"      (версия программы {VERSION})")
    print("=" * 70)
    print("  Опрашивается только этот блок. Проверяются четыре способа")
    print("  получить длинный ответ, между попытками делается пауза,")
    print("  чтобы блок успел освободиться.\n")

    for name in ("probe_result.csv", "elm_raw.log", "REPORT_TO_SEND.txt"):
        if os.path.exists(name):
            try:
                os.remove(name)
            except OSError:
                pass

    elm = open_link(args)
    if not elm:
        return 1

    log = CsvLog("target_result.csv", ["способ", "запрос", "кадры", "итог"])
    outcomes = []

    try:
        elm.accept_all()
        elm.set_header(tx_id)

        lines = elm.cmd("3E00", read_timeout=1.5)
        print(f"  Проверка связи: {' | '.join(lines) or 'нет ответа'}")
        if is_negative(lines):
            print("  [!] Блок не отвечает. Проверить зажигание и адаптер.")
            return 1
        source, _ = assemble_iso_tp(lines)
        print(f"  Блок на связи, отвечает с 0x{source:03X}\n" if source else "")

        request = "22F197"        # короткий запрос с заведомо длинным ответом

        MEANING = {
            0x01: "активна сейчас",
            0x02: "сбой в этом цикле",
            0x04: "ожидает проверки",
            0x08: "подтверждена",
            0x10: "не проверялась",
            0x20: "была ранее",
            0x40: "требует подтверждения",
            0x80: "предупреждение",
            0xFF: "все подряд",
        }

        # --- Короткие запросы: помещаются в один кадр, продолжение не нужно ---
        print("  --- Короткие запросы, без многокадрового обмена ---")

        count_lines = elm.cmd("1901FF", read_timeout=2.0)
        for line in count_lines:
            print(f"      {line}")
        _, count_data = assemble_iso_tp(count_lines)
        fault_count = None
        if count_data and count_data[0] == 0x59 and len(count_data) >= 6:
            fault_count = (count_data[4] << 8) | count_data[5]
            print(f"      ЧИСЛО ОШИБОК В БЛОКЕ: {fault_count}")
        else:
            print("      число ошибок получить не удалось")
        log.add(способ="счётчик ошибок", запрос="1901FF",
                кадры=" | ".join(count_lines),
                итог=f"ошибок: {fault_count}" if fault_count is not None else "нет данных")
        print()
        time.sleep(0.8)

        def analyse_masks(header):
            """Опрашивает список ошибок по каждому признаку."""
            print(f"  --- {header} ---")
            print("     признак              всего   первая запись\n")
            codes = {}
            counts = {}
            for bit in (0xFF, 0x01, 0x02, 0x08, 0x20, 0x04, 0x10, 0x40, 0x80):
                answer = elm.cmd(f"1902{bit:02X}", read_timeout=2.5)
                length = declared_length(answer)
                data = partial_payload(answer)
                if is_negative(answer) or not data or data[0] != 0x59:
                    note = " | ".join(answer) if answer else "нет ответа"
                    print(f"     {MEANING.get(bit, ''):<20} —       {note}")
                    log.add(способ=header, запрос=f"1902{bit:02X}",
                            кадры=" | ".join(answer), итог="ответа нет")
                    time.sleep(2.0)
                    continue
                total = (length - 3) // 4 if length and length >= 3 else 0
                counts[bit] = total
                records = data[3:]
                first_text = "—"
                if len(records) >= 3:
                    code = records[0:3]
                    first_text = f"{format_dtc(code)}  {failure_text(code)}"
                    codes[format_dtc(code)] = failure_text(code)
                print(f"     {MEANING.get(bit, ''):<20} {total:<7} {first_text}")
                log.add(способ=header, запрос=f"1902{bit:02X}",
                        кадры=" | ".join(answer),
                        итог=f"всего {total}, первая {first_text}")
                time.sleep(2.0)
            print()
            if codes:
                print("  Извлечённые коды:")
                for text, failure in codes.items():
                    print(f"     {text}  —  {failure}")
            print()
            return counts, codes

        # Выборка по признакам. Ответ обрывается на первом кадре, но
        # из него всё равно извлекается заявленная длина списка и первая
        # запись целиком — этого достаточно для диагноза.
        counts, codes = analyse_masks("Разбор списка ошибок по признакам")
        time.sleep(1.0)

        if getattr(args, "clear", False):
            print("=" * 70)
            print("  СТИРАНИЕ ПАМЯТИ ОШИБОК")
            print("=" * 70)
            print("  Зачем. Пока в памяти девять записей, ответ блока длинный")
            print("  и обрывается. После стирания вернутся только те ошибки,")
            print("  что возникают прямо сейчас — их будет одна-две, ответ")
            print("  уместится в один кадр, и коды прочитаются целиком.")
            print()
            # Стирание в обычной сессии блок запрещает, поэтому сначала
            # просим расширенную. Затем пробуем оба принятых формата команды.
            session = elm.cmd("1003", read_timeout=2.0)
            print(f"  Расширенная сессия: {' | '.join(session) or 'нет ответа'}")
            time.sleep(0.5)
            # Стирание работает только в расширенной сессии, иначе блок
            # отвечает отказом. В прошлом прогоне сессия не открывалась.
            elm.cmd("1003", read_timeout=2.0)
            time.sleep(0.3)
            answer = elm.cmd("14FFFFFF", read_timeout=3.0)
            if is_negative(answer) or "7F" in " ".join(answer):
                print("  Первый способ не прошёл, пробую сокращённый запрос...")
                time.sleep(0.5)
                answer = elm.cmd("04", read_timeout=3.0)
            print(f"  Ответ на стирание: {' | '.join(answer) or 'нет ответа'}")
            if is_negative(answer) or "7F" in " ".join(answer):
                time.sleep(1.0)
                answer = elm.cmd("14FF00", read_timeout=3.0)
                print(f"  Второй формат команды: {' | '.join(answer) or 'нет ответа'}")
            log.add(способ="стирание", запрос="14FFFFFF",
                    кадры=" | ".join(answer), итог="выполнено")
            print()
            print("  ТЕПЕРЬ СДЕЛАЙТЕ ТАК:")
            print("    1. Выключить зажигание, подождать 10 секунд")
            print("    2. Включить зажигание, включить заднюю передачу")
            print("    3. Подержать заднюю передачу 30 секунд")
            print("    4. Вернуться сюда и нажать Enter")
            print()
            try:
                input("  Нажмите Enter, когда будете готовы... ")
            except (EOFError, KeyboardInterrupt):
                pass
            print()
            counts2, codes2 = analyse_masks("Ошибки после стирания")
            new_codes = {k: v for k, v in codes2.items() if k not in codes}
            if new_codes:
                print("  НОВЫЕ КОДЫ, которых не было до стирания:")
                for text, failure in new_codes.items():
                    print(f"     {text}  —  {failure}")
                print()

        def attempt(title, steps):
            """Выполняет один способ передачи и печатает сырые кадры."""
            print(f"  --- {title} ---")
            collected = []
            for command, timeout in steps:
                answer = elm.cmd(command, read_timeout=timeout)
                if not command.startswith("AT"):
                    for line in answer:
                        print(f"      {line}")
                    collected += answer
            _, data = assemble_iso_tp(collected)
            complete = bool(data) and not is_truncated(collected) and data[0] != 0x7F
            verdict = "ПОЛНЫЙ ОТВЕТ" if complete else "не вышло"
            if data:
                text = "".join(chr(b) if 32 <= b < 127 else "." for b in data[3:])
                print(f"      разобрано: {data.hex().upper()}  {text}")
            print(f"      -> {verdict}\n")
            log.add(способ=title, запрос=request,
                    кадры=" | ".join(collected), итог=verdict)
            outcomes.append((title, complete, data))
            time.sleep(1.5)
            return complete, data

        # Способ 1. Запрос обычным путём, разрешение — сырым кадром.
        # Раньше не пробовался: переключение режима между запросом
        # и разрешением укладывается в отведённое блоку время ожидания.
        elm.cmd("ATCAF1")
        ok, data = attempt("1. запрос обычный, разрешение сырым кадром", [
            (request, 2.0),
            ("ATCAF0", 1.0),
            ("3000000000000000", 2.5),
            ("ATCAF1", 1.0),
        ])
        if ok:
            return finish_target(elm, log, outcomes, tx_id)

        # Способ 2. Полностью сырые кадры, добивка нулями.
        elm.cmd("ATCAF0")
        elm.cmd("ATSTFF")
        attempt("2. сырые кадры, добивка нулями", [
            ("0322F19700000000", 2.5),
            ("3000000000000000", 2.5),
        ])

        # Способ 3. Сырые кадры, добивка байтами AA — так добивает сам VAG.
        attempt("3. сырые кадры, добивка AA", [
            ("0322F197AAAAAAAA", 2.5),
            ("3000000000000000", 2.5),
        ])

        # Способ 4. Сырые кадры без добивки — адаптер дополнит сам.
        attempt("4. сырые кадры без добивки", [
            ("0322F197", 2.5),
            ("3000000000000000", 2.5),
        ])
        elm.cmd("ATCAF1")
        elm.cmd("ATST32")

        return finish_target(elm, log, outcomes, tx_id)
    finally:
        elm.close()


def finish_target(elm, log, outcomes, tx_id):
    """Подводит итог целевого опроса."""
    log.close()
    print("=" * 70)
    print("ИТОГ")
    print("=" * 70)
    winners = [title for title, ok, _ in outcomes if ok]
    if winners:
        print(f"Сработал способ: {winners[0]}")
        print("Дальше этим способом можно читать ошибки блока.")
    else:
        print("Ни один способ не дал полного ответа.")
        print("Значит, прошивка адаптера многокадровый обмен с этим блоком")
        print("не тянет, и нужен другой интерфейс.")
    build_bundle(elm)
    print()
    print("  " + "=" * 62)
    print("  ПРИШЛИТЕ ОДИН ФАЙЛ:  REPORT_TO_SEND.txt")
    print("  " + "=" * 62)
    return 0


def mode_monitor(args):
    """
    Монитор активных ошибок для поиска неисправного канала перебором.

    Смысл. Полный список ошибок этот адаптер прочитать не может, но
    ЧИСЛО ошибок берётся из заголовка ответа и читается всегда. Этого
    достаточно: отключаем все датчики, подключаем по одному и смотрим,
    после какого число не уменьшилось. Тот канал и неисправен.
    """
    tx_id = int(args.id, 16) if args.id else TARGET_DEFAULT

    print("=" * 70)
    print(f"МОНИТОР АКТИВНЫХ ОШИБОК БЛОКА 0x{tx_id:03X}"
          f"      (версия {VERSION})")
    print("=" * 70)
    print("""
  ПОРЯДОК РАБОТЫ

    1. Зажигание включено, задняя передача включена, двигатель заглушен
    2. Отключить все четыре задних датчика
    3. Запомнить число активных ошибок — это отправная точка
    4. Подключать датчики по одному, каждый раз ожидая обновления счётчика

  КАК ЧИТАТЬ РЕЗУЛЬТАТ

    Число уменьшилось после подключения  -> этот канал исправен
    Число НЕ уменьшилось                 -> неисправность в этом канале

  Счётчик обновляется каждые несколько секунд. При изменении звучит сигнал.
  Остановить: Ctrl+C
""")

    elm = open_link(args)
    if not elm:
        return 1

    log = CsvLog("monitor_log.csv",
                 ["время", "секунда", "активных", "всего", "первый_код",
                  "событие"])
    try:
        elm.accept_all()
        elm.set_header(tx_id)

        lines = elm.cmd("3E00", read_timeout=1.5)
        if is_negative(lines):
            print("  [!] Блок не отвечает. Проверить зажигание и адаптер.")
            return 1
        print("  Блок на связи. Начинаю опрос.\n")
        print("     время      активных   всего   первый активный код")
        print("     " + "-" * 60)

        started = time.time()
        previous = None
        cycle = 0

        def ask_count(mask):
            """Возвращает число ошибок и первый код по заданному признаку."""
            answer = elm.cmd(f"1902{mask:02X}", read_timeout=2.5)
            if is_negative(answer):
                return None, None
            length = declared_length(answer)
            data = partial_payload(answer)
            if not data or data[0] != 0x59 or length is None:
                return None, None
            total = (length - 3) // 4 if length >= 3 else 0
            code_text = None
            records = data[3:]
            if len(records) >= 3:
                code = records[0:3]
                code_text = f"{format_dtc(code)} {failure_text(code)}"
            return total, code_text

        while True:
            cycle += 1
            active, first_code = ask_count(0x01)
            time.sleep(1.8)          # дать блоку закрыть оборванную передачу

            total = ""
            if cycle % 4 == 1:
                total_value, _ = ask_count(0xFF)
                total = total_value if total_value is not None else ""
                time.sleep(1.8)

            elapsed = time.time() - started
            stamp = timestamp()

            if active is None:
                print(f"     {stamp}   нет ответа")
                log.add(время=stamp, секунда=f"{elapsed:.1f}", активных="",
                        всего=total, первый_код="", событие="нет ответа")
            else:
                event = ""
                if previous is not None and active != previous:
                    direction = "стало меньше" if active < previous else "стало больше"
                    event = f"{direction}: было {previous}"
                    beep("alert")
                print(f"     {stamp}   {active:<10} {str(total):<7} "
                      f"{first_code or '—'}   {event}")
                log.add(время=stamp, секунда=f"{elapsed:.1f}", активных=active,
                        всего=total, первый_код=first_code or "",
                        событие=event)
                previous = active

    except KeyboardInterrupt:
        print("\n  [остановлено]")
    finally:
        log.close()
        dump_raw_log(elm)
        build_bundle(elm)
        elm.close()

    print()
    print("  Журнал: monitor_log.csv")
    print("  Файл для отправки: REPORT_TO_SEND.txt")
    return 0


def mode_probe(args):
    """
    Детальный опрос одного блока: расширенная сессия, вся идентификация,
    ошибки двумя способами и СЫРОЙ ответ в шестнадцатеричном виде.

    Сырой ответ нужен, чтобы проверить правильность разбора. Если разбор
    ошибается, по сырым байтам это сразу видно, а по красивой таблице — нет.
    """
    if getattr(args, "all", False):
        config = load_config()
        modules = config.get("modules", [])
        if not modules:
            print("[!] Список блоков пуст. Сначала выполнить пункт 1 — поиск блоков.")
            return 1

        print("=" * 70)
        print(f"РЕЖИМ PROBE — обход всех блоков ({len(modules)} шт.)"
              f"      (версия программы {VERSION})")
        print("=" * 70)
        for name in ("probe_result.csv", "elm_raw.log"):
            if os.path.exists(name):
                try:
                    os.remove(name)
                except OSError:
                    pass

        elm = open_link(args)
        if not elm:
            return 1
        try:
            elm.accept_all()          # один раз на весь обход
            log = CsvLog("probe_result.csv",
                         ["блок", "запрос", "ответ_сырой", "расшифровка"])
            results = []
            for item in modules:
                results.append(probe_one(elm, int(item["id"]), log))
                elm.auto_receive()    # снять настройку под предыдущий блок
                time.sleep(0.25)      # дать адаптеру перевести дух
            log.close()

            print("\n" + "=" * 70)
            print("СВОДКА")
            print("=" * 70)
            for item in results:
                label = item["name"] or "—"
                print(f"  0x{item['id']:03X}  {label:<28} "
                      f"ошибок: {len(item['faults'])}")

            keywords = ("PAR", "PDC", "EPH", "ASSIST", "AREA", "UMFELD",
                        "EINPARK", "OPS")
            # 0x700 — широковещательный адрес, на нём отвечают сразу
            # несколько блоков и их имена смешиваются. В кандидаты не берём.
            hits = [i for i in results
                    if i["id"] != 0x700
                    and any(w in i["name"].upper() for w in keywords)]
            print()
            if hits:
                for item in hits:
                    print(f"  Похоже на парктроник: 0x{item['id']:03X} "
                          f"— {item['name']}")
            else:
                print("  Блока с признаками парковочной системы не найдено.")
            dump_raw_log(elm)
            bundle = build_bundle(elm, results)
            print("\n  Отчёт: probe_result.csv")
            print()
            print("  " + "=" * 62)
            if bundle:
                print(f"  ПРИШЛИТЕ ОДИН ФАЙЛ:  {bundle}")
                print("  В нём уже есть версия, время, итоги и весь обмен.")
            print("  " + "=" * 62)
            return 0
        finally:
            elm.close()

    tx_id = resolve_tx_id(args)
    print("=" * 70)
    print(f"РЕЖИМ PROBE — детальный опрос блока 0x{tx_id:03X}")
    print("=" * 70)

    elm = open_link(args)
    if not elm:
        return 1
    try:
        elm.accept_all()
        uds = UdsClient(elm, tx_id)

        if not uds.tester_present():
            print("\n[!] Блок не отвечает на этом адресе.")
            return 1
        print("\n  Блок на связи.")

        opened = uds.start_session(0x03)
        print(f"  Расширенная сессия: {'открыта' if opened else 'не открылась'}")

        log = CsvLog("probe_result.csv", ["блок", "запрос", "ответ_сырой",
                                          "расшифровка"])

        print("\n  --- Идентификация ---")
        wide_dids = dict(INFO_DIDS)
        wide_dids.update({0xF19E: "имя файла описания", 0xF1A2: "код версии",
                          0xF186: "текущая сессия", 0xF192: "номер поставщика",
                          0xF193: "версия железа", 0xF195: "версия сборки"})
        for did, title in wide_dids.items():
            try:
                value = uds.read_did(did, read_timeout=1.0)
            except (UdsError, OSError):
                continue
            text = "".join(chr(b) if 32 <= b < 127 else "." for b in value)
            print(f"  {did:04X}  {title:<26} {text}")
            log.add(блок=f"0x{tx_id:03X}", запрос=f"22{did:04X}",
                    ответ_сырой=value.hex().upper(), расшифровка=text)

        print("\n  --- Ошибки, способ UDS (сервис 19) ---")
        try:
            raw = uds.read_dtcs_raw()
            print(f"  Сырой ответ: {raw.hex().upper()}")
            log.add(блок=f"0x{tx_id:03X}", запрос="1902FF",
                    ответ_сырой=raw.hex().upper(), расшифровка="сырые байты")
            records = raw[3:]
            print(f"  Байт после заголовка: {len(records)}"
                  f"  (делится на 4 без остатка: "
                  f"{'да' if len(records) % 4 == 0 else 'НЕТ — разбор под вопросом'})")
            for offset in range(0, len(records) - 3, 4):
                code = records[offset:offset + 3]
                status = records[offset + 3]
                print(f"    {format_dtc(code)}  {failure_text(code)}"
                      f"  [{describe_status(status)}]")
                log.add(блок=f"0x{tx_id:03X}", запрос="1902FF",
                        ответ_сырой=code.hex().upper() + f"{status:02X}",
                        расшифровка=f"{format_dtc(code)} / {failure_text(code)}")
        except (UdsError, OSError) as exc:
            print(f"  Не удалось: {exc}")

        print("\n  --- Ошибки, способ KWP (сервис 18) ---")
        try:
            faults = uds.read_dtcs_kwp()
            print(f"  Записей: {len(faults)}")
            for code, status in faults:
                print(f"    {code[:2].hex().upper()}  статус 0x{status:02X}")
                log.add(блок=f"0x{tx_id:03X}", запрос="1802FF00",
                        ответ_сырой=code[:2].hex().upper() + f"{status:02X}",
                        расшифровка="запись KWP")
        except (UdsError, OSError) as exc:
            print(f"  Не удалось: {exc}")

        print("\n  --- Сырые кадры от адаптера ---")
        print("  По ним видно структуру ответа и адрес, с которого он пришёл.\n")
        for request_hex, title in (("22F187", "номер запчасти"),
                                   ("22F197", "название блока"),
                                   ("1902FF", "список ошибок")):
            elm.set_header(tx_id)
            lines = elm.cmd(request_hex, read_timeout=3.0)
            print(f"  Запрос {request_hex} ({title}):")
            if not lines:
                print("    нет ответа")
            for line in lines:
                print(f"    {line}")
            flag = "ОБРЕЗАН" if is_truncated(lines) else "полный"
            print(f"    -> ответ {flag}\n")
            log.add(блок=f"0x{tx_id:03X}", запрос=request_hex,
                    ответ_сырой=" ".join(lines), расшифровка=f"кадры, {flag}")

        log.close()
        dump_raw_log(elm)
        print("\n  Отчёт: probe_result.csv")
        print("  Полный обмен с адаптером: elm_raw.log")
        print("  Пришлите оба файла.")
        return 0
    finally:
        elm.close()


# ---------------------------------------------------------------------------
# Разбор командной строки
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="pdc_diag.py",
        description="Диагностика парковочной системы VW Polo Sedan через ELM327 Wi-Fi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Порядок работы:
  1. python pdc_diag.py scan              найти блок и протокол
  2. python pdc_diag.py dtc               прочитать ошибки
  3. python pdc_diag.py dtc --clear       стереть и проверить, какие вернутся
  4. python pdc_diag.py dids              найти идентификаторы живых данных
  5. python pdc_diag.py watch --dids ...  понять, какой из них какой датчик
  6. python pdc_diag.py shake             провокационный тест
""")

    config = load_config()
    parser.add_argument("--host", default=config.get("host", DEFAULT_HOST),
                        help="адрес адаптера")
    parser.add_argument("--port", type=int, default=config.get("port", DEFAULT_PORT),
                        help="порт адаптера")
    parser.add_argument("--id", help="идентификатор блока в шестнадцатеричном виде")
    parser.add_argument("--verbose", action="store_true",
                        help="печатать весь обмен с адаптером")

    subparsers = parser.add_subparsers(dest="mode", required=True)

    scan = subparsers.add_parser("scan", help="найти блоки на шине")
    scan.add_argument("--quick", action="store_true",
                      help="короткий список вместо полного скана")
    scan.set_defaults(func=mode_scan)

    info = subparsers.add_parser("info", help="идентификация блока")
    info.set_defaults(func=mode_info)

    dtc = subparsers.add_parser("dtc", help="прочитать ошибки")
    dtc.add_argument("--clear", action="store_true",
                     help="стереть память ошибок после чтения")
    dtc.set_defaults(func=mode_dtc)

    dids = subparsers.add_parser("dids", help="найти идентификаторы живых данных")
    dids.add_argument("--range", default="1000-10FF",
                      help="диапазон поиска, например 1000-10FF")
    dids.set_defaults(func=mode_dids)

    watch = subparsers.add_parser("watch", help="следить за живыми данными")
    watch.add_argument("--dids", required=True,
                       help="список через запятую, например 1001,1002")
    watch.add_argument("--interval", type=float, default=0.3,
                       help="пауза между опросами в секундах")
    watch.set_defaults(func=mode_watch)

    shake = subparsers.add_parser("shake", help="провокационный тест")
    shake.add_argument("--minutes", type=float, default=10.0,
                       help="длительность теста в минутах")
    shake.add_argument("--interval", type=float, default=0.25,
                       help="пауза между опросами связи")
    shake.add_argument("--dtc-interval", type=float, default=2.0,
                       dest="dtc_interval",
                       help="как часто перечитывать ошибки, секунды")
    shake.add_argument("--dids", help="дополнительно следить за данными")
    shake.set_defaults(func=mode_shake)

    monitor = subparsers.add_parser("monitor", help="живой счётчик ошибок")
    monitor.add_argument("--interval", type=float, default=2.5,
                         help="пауза между опросами в секундах")
    monitor.set_defaults(func=mode_monitor)

    target = subparsers.add_parser("target", help="целевой опрос одного блока")
    target.add_argument("--clear", action="store_true",
                        help="стереть ошибки и перечитать их заново")
    target.set_defaults(func=mode_target)

    probe = subparsers.add_parser("probe", help="детальный опрос блока")
    probe.add_argument("--all", action="store_true",
                       help="обойти все найденные блоки подряд")
    probe.set_defaults(func=mode_probe)

    full = subparsers.add_parser("full", help="полный отчёт по всем блокам")
    full.set_defaults(func=mode_full)

    return parser


def main():
    # Консоль Windows может не поддерживать часть символов.
    # Заменяем непечатаемые вместо аварийного завершения программы.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[прервано]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
