#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdc_diag.py — диагностическая программа для парковочной системы VW Polo Sedan.
Версия 1.0

Работает через адаптер ELM327 Wi-Fi. Написана под конкретную задачу:
найти плавающий контакт в проводке парктроника, который проявляется
при шевелении жгута и который штатные сканеры ловят плохо.

Режимы работы:
    scan   — найти блоки на шине и определить протокол
    info   — идентификация блока парктроника
    dtc    — прочитать ошибки, при желании стереть
    dids   — найти идентификаторы живых данных перебором
    watch  — следить за выбранными данными в реальном времени
    shake  — ПРОВОКАЦИОННЫЙ ТЕСТ: ловит момент пропадания контакта

Главный режим — shake. Вы шевелите жгут двумя руками, программа опрашивает
блок в цикле, пищит при появлении ошибки или потере связи и пишет всё
в CSV с метками времени. Потом по логу видно: «на 47-й секунде датчик
пропал на 300 мс» — а вы помните, что именно трясли в этот момент.

Все режимы кроме явного стирания ошибок работают ТОЛЬКО НА ЧТЕНИЕ.
Кодирование, адаптации и запись в блоки не выполняются.

Требования: Python 3.8+, только стандартная библиотека.

Примеры:
    python pdc_diag.py scan
    python pdc_diag.py dtc
    python pdc_diag.py dtc --clear
    python pdc_diag.py dids --range 1000-10FF
    python pdc_diag.py watch --dids 1001,1002,1003
    python pdc_diag.py shake --minutes 10
"""

import argparse
import csv
import os
import socket
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Настройки по умолчанию
# ---------------------------------------------------------------------------
VERSION = "5.0"          # версия программы, печатается в шапке каждого режима

DEFAULT_HOST = "192.168.0.10"
DEFAULT_PORT = 35000

PARKING_AID_ADDR = 0x76          # адрес блока парктроника в номенклатуре VAG
DEFAULT_TX_ID = 0x700 + PARKING_AID_ADDR

NEGATIVE_TOKENS = ("NO DATA", "CAN ERROR", "BUS INIT", "UNABLE TO CONNECT",
                   "BUS BUSY", "FB ERROR", "DATA ERROR", "STOPPED",
                   "BUFFER FULL", "ERROR", "?")

# Расшифровка кодов отрицательного ответа UDS (сервис 0x7F)
NRC_TEXT = {
    0x10: "общий отказ",
    0x11: "сервис не поддерживается",
    0x12: "подфункция не поддерживается",
    0x13: "неверная длина запроса",
    0x21: "блок занят, нужен повтор запроса",
    0x22: "условия не выполнены",
    0x31: "запрос вне диапазона (такого идентификатора нет)",
    0x33: "требуется доступ по паролю",
    0x78: "ответ будет позже",
    0x7F: "сервис недоступен в текущей сессии",
}


# ---------------------------------------------------------------------------
# Звук
# ---------------------------------------------------------------------------
def beep(kind="alert"):
    """Короткий звуковой сигнал. На Windows через winsound, иначе через терминал."""
    try:
        import winsound
        freq, dur = (1200, 180) if kind == "alert" else (600, 120)
        winsound.Beep(freq, dur)
        return
    except Exception:
        pass
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Транспорт: адаптер ELM327 по TCP
# ---------------------------------------------------------------------------
class Elm327:
    """Обёртка над сокетом адаптера. Отвечает только за обмен строками."""

    def __init__(self, host, port, timeout=6.0, verbose=False):
        self.host, self.port = host, port
        self.timeout = timeout
        self.verbose = verbose
        self.sock = None
        self.current_header = None
        self.raw_log = []
        self.unsupported = []
        self.empty_streak = 0        # подряд идущие пустые ответы
        self.recoveries = 0          # сколько раз пришлось перезапускать

    # -- соединение ---------------------------------------------------------
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        time.sleep(0.3)
        self._drain()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _drain(self):
        self.sock.settimeout(0.2)
        try:
            while self.sock.recv(4096):
                pass
        except (socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)

    # -- обмен --------------------------------------------------------------
    def cmd(self, text, read_timeout=1.2):
        """Отправляет команду, читает ответ до приглашения '>'."""
        if self.sock is None:
            raise OSError("нет соединения с адаптером")

        # Чистим буфер от кадров, не забранных предыдущим запросом.
        # Без этого ответ одного блока может быть прочитан как ответ
        # следующего — при опросе подряд это даёт путаницу имён.
        self.sock.settimeout(0.03)
        try:
            while self.sock.recv(4096):
                pass
        except (socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)

        self.sock.sendall((text + "\r").encode("ascii"))

        buf = bytearray()
        deadline = time.time() + read_timeout
        self.sock.settimeout(0.2)
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if b">" in buf:
                break
        self.sock.settimeout(self.timeout)

        if not buf:
            # Адаптер ничего не прислал в отведённое окно. Дешёвые модели
            # захлёбываются при плотном потоке команд и отвечают с задержкой.
            # Даём вторую попытку дочитать, не посылая команду заново.
            self.sock.settimeout(0.3)
            deadline = time.time() + 1.5
            while time.time() < deadline:
                try:
                    chunk = self.sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if b">" in buf:
                    break
            self.sock.settimeout(self.timeout)

        raw = buf.decode("ascii", errors="replace")

        # Самовосстановление. Дешёвые адаптеры перестают отвечать после
        # нескольких десятков команд подряд. Раньше это выглядело как
        # «блоки молчат» и вело к неверным выводам. Теперь программа
        # замечает череду пустых ответов и перезапускает адаптер сама.
        if raw.strip() == "" and not text.startswith("AT"):
            self.empty_streak += 1
            if self.empty_streak >= 3 and self.recoveries < 5:
                self.empty_streak = 0
                self.recoveries += 1
                self._recover()
        else:
            self.empty_streak = 0

        if raw.strip().startswith("?"):
            # Адаптер не понял команду. Важно видеть это сразу:
            # непонятая команда молча не выполняется, а программа
            # продолжает считать, что настройка применена.
            self.unsupported.append(text)
        self.raw_log.append((text, raw))
        if self.verbose:
            print(f"      >> {text}   << {raw!r}")

        lines = []
        for line in raw.replace(">", "").replace("\n", "\r").split("\r"):
            line = line.strip()
            if line and line.upper() != "SEARCHING...":
                lines.append(line)
        return lines

    # -- инициализация ------------------------------------------------------
    def init(self):
        """Приводит адаптер в предсказуемое состояние."""
        self.cmd("ATZ", read_timeout=3.0)
        time.sleep(0.5)
        for command in ("ATE0",      # без эха
                        "ATL0",      # без лишних переводов строки
                        "ATS0",      # без пробелов в ответе
                        "ATH1",      # показывать идентификаторы кадров
                        "ATSP6",     # ISO 15765-4 CAN 11 бит 500 кбит/с
                        "ATCAF1",    # автосборка ISO-TP
                        "ATAT0",     # предсказуемые тайминги
                        "ATST32"):   # ожидание ответа около 200 мс
            self.cmd(command)

    def _recover(self):
        """Перезапускает адаптер, не разрывая соединения."""
        try:
            self.raw_log.append(("[самовосстановление]", "адаптер перезапускается"))
            self.sock.sendall(b"ATZ\r")
            time.sleep(1.2)
            self._drain()
            for command in ("ATE0", "ATL0", "ATS0", "ATH1", "ATSP6",
                            "ATCAF1", "ATAT0", "ATST32", "ATCF000", "ATCM000"):
                self.sock.sendall((command + "\r").encode("ascii"))
                time.sleep(0.08)
            self._drain()
            self.current_header = None
            self.rx_filter = None
        except OSError:
            pass

    def reset_state(self):
        """
        Возвращает адаптер в исходный режим без полного перезапуска.

        Нужно между блоками: работа с одним блоком меняет режим сборки
        кадров и длительность ожидания, и эти настройки утекают
        на следующий блок, отчего он выглядит молчащим.
        """
        for command in ("ATE0", "ATL0", "ATS0", "ATH1",
                        "ATCAF1", "ATAT0", "ATST32"):
            self.cmd(command)
        self.current_header = None
        self.rx_filter = None
        self.accept_all()

    def identify(self):
        version = " ".join(self.cmd("ATI")) or "нет ответа"
        voltage = " ".join(self.cmd("ATRV")) or "нет ответа"
        return version, voltage

    def set_header(self, can_id):
        """
        Устанавливает идентификатор отправляемых кадров. Больше ничего.

        Раньше здесь дополнительно настраивались параметры кадра
        управления потоком. От этого пришлось отказаться: режим поиска
        блоков, где этих команд нет, находил все блоки, а детальный опрос
        с ними терял блоки кузовной электроники. Дешёвые адаптеры
        обрабатывают эти команды неверно. Продолжение длинного ответа
        программа запрашивает сама, в режиме сырых кадров.
        """
        if self.current_header != can_id:
            self.cmd(f"ATSH{can_id:03X}")
            self.current_header = can_id

    def accept_all(self):
        """
        Принимать кадры с любым идентификатором.

        Нулевая маска означает «сравнивать нечего», то есть пропускать
        любые кадры. Этим же снимается адресный фильтр, если он был
        выставлен под конкретный блок.
        """
        self.cmd("ATCF000")
        self.cmd("ATCM000")
        self.rx_filter = None

    def apply_mf_strategy(self, name, tx_id, rx_id):
        """
        Применяет один из способов заставить адаптер дочитывать длинный ответ.

        Способы перебираются по очереди, потому что дешёвые адаптеры
        поддерживают их выборочно и предсказать заранее нельзя.
        """
        if name == "cra":
            # только указание адреса ответа, управление потоком автоматическое
            self.cmd(f"ATCRA{rx_id:03X}")
        elif name == "cra_fc":
            # адрес ответа плюс заданные вручную параметры кадра разрешения
            self.cmd(f"ATCRA{rx_id:03X}")
            self.cmd(f"ATFCSH{tx_id:03X}")
            self.cmd("ATFCSD300000")
            self.cmd("ATFCSM1")

    def setup_multiframe(self, tx_id, rx_id):
        """
        Готовит адаптер к длинному ответу от блока с нестандартным адресом.

        Адаптер сам обслуживает многокадровый обмен только для пары
        «запрос 0x7E0 — ответ 0x7E8». Для блоков кузова, где ответ
        приходит со смещением (0x70A отвечает с 0x774), ему нужно явно
        указать ожидаемый адрес ответа. Без этого он считает такие кадры
        посторонним трафиком и не запрашивает продолжение.
        """
        self.cmd(f"ATCRA{rx_id:03X}")     # ожидаемый адрес ответа
        self.cmd(f"ATFCSH{tx_id:03X}")    # чем адресовать кадр разрешения
        self.cmd("ATFCSD300000")          # разрешить передачу без задержек
        self.cmd("ATFCSM1")               # применять заданные параметры

    def auto_receive(self):
        """
        Возвращает приём «всё подряд» после точечной настройки на блок.

        Перебраны три способа, работает только третий:
          ATAR      — заставляет ждать ответ по правилу «запрос плюс
                      восемь», блоки кузова после этого не слышны;
          ATCRAXXX  — маска «любой адрес», дешёвый клон отвечает «?»;
          ATCF/ATCM — задание фильтра и маски напрямую. Команда ATCRA
                      это просто их сокращённая запись, поэтому обнуление
                      маски снимает и её.
        """
        self.cmd("ATCF000")
        self.cmd("ATCM000")
        self.cmd("ATFCSM0")     # вернуть автоматическое управление потоком
        self.rx_filter = None

    def set_rx_filter(self, can_id):
        """
        Задаёт идентификатор, с которого ожидается ответ.

        Это принципиально для длинных ответов: адаптер посылает блоку
        разрешение на продолжение только если узнаёт входящий первый кадр
        по фильтру. При приёме «всё подряд» он этого не делает, и длинный
        ответ обрывается на первом кадре.
        """
        if getattr(self, "rx_filter", None) != can_id:
            self.cmd(f"ATCRA{can_id:03X}")
            self.rx_filter = can_id


# ---------------------------------------------------------------------------
# Разбор ответов
# ---------------------------------------------------------------------------
def is_negative(lines):
    if not lines:
        return True
    joined = " ".join(lines).upper()
    return any(token in joined for token in NEGATIVE_TOKENS)


def split_id_and_data(line):
    """
    Разбирает строку ответа адаптера на идентификатор и байты данных.
    Формат при ATH1 и ATS0: 3 шестнадцатеричных символа идентификатора,
    затем байты подряд. Пробелы, если они есть, игнорируются.
    """
    token = line.replace(" ", "").strip().upper()
    if len(token) < 5:
        return None, b""
    can_id_text, payload_text = token[:3], token[3:]
    try:
        can_id = int(can_id_text, 16)
    except ValueError:
        return None, b""
    if len(payload_text) % 2:
        payload_text = payload_text[:-1]
    try:
        payload = bytes.fromhex(payload_text)
    except ValueError:
        return None, b""
    return can_id, payload


def assemble_iso_tp(lines):
    """
    Собирает полезную нагрузку из кадров ISO-TP.
    Понимает одиночный кадр, первый кадр и последующие.
    Возвращает (can_id, данные) либо (None, b'').
    """
    single, first, consecutive = None, None, []
    source_id = None

    for line in lines:
        can_id, payload = split_id_and_data(line)
        if can_id is None or not payload:
            continue
        if source_id is None:
            source_id = can_id
        elif can_id != source_id:
            # Кадры от другого блока. На широковещательных адресах
            # отвечают сразу несколько блоков, и их ответы перемешиваются.
            # Берём только первый источник.
            continue
        pci_type = payload[0] >> 4
        if pci_type == 0x0:                      # одиночный кадр
            length = payload[0] & 0x0F
            single = payload[1:1 + length]
        elif pci_type == 0x1:                    # первый кадр
            length = ((payload[0] & 0x0F) << 8) | payload[1]
            first = (length, payload[2:])
        elif pci_type == 0x2:                    # последующий кадр
            consecutive.append(payload[1:])

    if single is not None:
        return source_id, single
    if first is not None:
        length, data = first
        for chunk in consecutive:
            data += chunk
        if len(data) < length:
            # Пришёл только первый кадр: адаптер не запросил продолжение.
            # Возвращаем что есть, но помечаем ответ как неполный.
            return source_id, data + b"\xFF" * 0
        return source_id, data[:length]
    return None, b""


def declared_length(lines):
    """
    Возвращает заявленную длину ответа из заголовка первого кадра.
    Для одиночного кадра — его длину. None, если разобрать нечего.
    """
    for line in lines:
        can_id, payload = split_id_and_data(line)
        if can_id is None or not payload:
            continue
        pci_type = payload[0] >> 4
        if pci_type == 0x1:
            return ((payload[0] & 0x0F) << 8) | payload[1]
        if pci_type == 0x0:
            return payload[0] & 0x0F
    return None


def partial_payload(lines):
    """
    Собирает то, что реально пришло, даже если ответ оборван.
    Нужно, чтобы вытащить хотя бы первые записи из длинного списка.
    """
    data = b""
    source = None
    for line in lines:
        can_id, payload = split_id_and_data(line)
        if can_id is None or not payload:
            continue
        if source is None:
            source = can_id
        elif can_id != source:
            continue
        pci_type = payload[0] >> 4
        if pci_type == 0x0:
            data += payload[1:1 + (payload[0] & 0x0F)]
        elif pci_type == 0x1:
            data += payload[2:]
        elif pci_type == 0x2:
            data += payload[1:]
    return data


def is_truncated(lines):
    """
    True, если в ответе есть первый кадр многокадровой посылки,
    но собранных байт меньше заявленной длины.
    """
    first_len, collected = None, 0
    for line in lines:
        can_id, payload = split_id_and_data(line)
        if can_id is None or not payload:
            continue
        pci_type = payload[0] >> 4
        if pci_type == 0x1:
            first_len = ((payload[0] & 0x0F) << 8) | payload[1]
            collected += len(payload) - 2
        elif pci_type == 0x2:
            collected += len(payload) - 1
    return first_len is not None and collected < first_len


# ---------------------------------------------------------------------------
# Клиент UDS
# ---------------------------------------------------------------------------
def build_single_frame(payload):
    """Собирает одиночный кадр ISO-TP из полезной нагрузки, с добиванием до 8 байт."""
    frame = bytes([len(payload)]) + payload
    return frame.ljust(8, b"\x00")


FLOW_CONTROL = "3000000000000000"   # разрешить передачу всего, без задержек


class UdsError(Exception):
    """Отрицательный ответ блока или отсутствие ответа."""


class UdsClient:
    """Минимальный клиент UDS поверх ELM327."""

    def __init__(self, elm, tx_id):
        self.elm = elm
        self.tx_id = tx_id
        self.rx_id = None
        self.mf_tried = set()
        self.mf_strategy = None
        self.needs_manual = False

    def _retry_long(self, payload_hex, read_timeout):
        """
        Пробует по очереди все известные способы получить длинный ответ.
        Возвращает данные либо None. Сработавший способ запоминается.
        """
        if self.mf_strategy:
            order = [self.mf_strategy]
        else:
            order = [name for name in ("cra", "cra_fc")
                     if name not in self.mf_tried]

        for name in order:
            self.mf_tried.add(name)
            self.elm.apply_mf_strategy(name, self.tx_id, self.rx_id)
            time.sleep(0.25)
            lines = self.elm.cmd(payload_hex, read_timeout=read_timeout + 1.0)
            if is_negative(lines) or is_truncated(lines):
                continue
            _, data = assemble_iso_tp(lines)
            if data and data[0] != 0x7F:
                self.mf_strategy = name
                return data

        if "manual" not in self.mf_tried:
            self.mf_tried.add("manual")
            try:
                data = self.request_manual(payload_hex)
                self.mf_strategy = "manual"
                return data
            except (UdsError, OSError):
                pass
        return None

    def request_manual(self, payload_hex, read_timeout=2.5, attempts=3):
        """
        Запрос в режиме сырых кадров с самостоятельной отправкой разрешения.

        Зачем это нужно. Адаптер ведёт многокадровый обмен только с парой
        «запрос 0x7E0 — ответ 0x7E8», зашитой в прошивку. Блокам кузова,
        которые отвечают с другим смещением, разрешение на продолжение
        никто не посылает: блок отдаёт первый кадр и ждёт. Все следующие
        запросы он отклоняет с ответом «занят», потому что предыдущая
        передача так и не завершилась. Поэтому здесь кадр разрешения
        отправляется вручную, сразу за первым кадром ответа.
        """
        elm = self.elm
        elm.cmd("ATCF000")
        elm.cmd("ATCM000")
        elm.cmd("ATCAF0")          # работаем сырыми кадрами
        elm.cmd("ATSTFF")          # длинное окно ожидания
        try:
            elm.current_header = None
            elm.set_header(self.tx_id)
            frame = build_single_frame(bytes.fromhex(payload_hex))

            for attempt in range(attempts):
                lines = elm.cmd(frame.hex().upper(), read_timeout=read_timeout)

                if is_negative(lines):
                    # Блок мог остаться занятым после прошлой незавершённой
                    # передачи — даём ему время освободиться
                    time.sleep(0.6)
                    continue

                collected = list(lines)
                if is_truncated(lines):
                    extra = elm.cmd(FLOW_CONTROL, read_timeout=read_timeout)
                    collected += extra

                source, data = assemble_iso_tp(collected)
                if not data:
                    time.sleep(0.4)
                    continue
                if data[0] == 0x7F:
                    nrc = data[2] if len(data) > 2 else 0
                    if nrc in (0x21, 0x78):
                        time.sleep(0.6)
                        continue
                    raise UdsError(f"отказ блока: {NRC_TEXT.get(nrc, hex(nrc))}")
                if source is not None:
                    self.rx_id = source
                return data

            raise UdsError("нет ответа от блока в режиме сырых кадров")
        finally:
            elm.cmd("ATCAF1")
            elm.cmd("ATST32")
            elm.current_header = None

    def request(self, payload_hex, read_timeout=1.2, attempts=3):
        """
        Отправляет запрос и возвращает байты положительного ответа.

        Повторяет запрос, если блок отвечает «занят» (код 0x21) или
        «ответ будет позже» (код 0x78). Блоки кузовной электроники VAG
        часто отвечают так на первый запрос после пробуждения, и без
        повтора программа делает ложный вывод, что блок недоступен.
        """
        if self.needs_manual:
            data = self.request_manual(payload_hex)
            self.mf_strategy = "сырые кадры"
            return data

        last_error = "нет ответа от блока"

        for attempt in range(attempts):
            self.elm.set_header(self.tx_id)
            lines = self.elm.cmd(payload_hex, read_timeout=read_timeout)

            if is_negative(lines):
                last_error = "нет ответа от блока"
                time.sleep(0.3)
                continue

            if is_truncated(lines) and self.rx_id:
                # Пришёл только первый кадр. Перебираем способы заставить
                # адаптер дочитать остальное, пока какой-нибудь не сработает.
                full = self._retry_long(payload_hex, read_timeout)
                if full is not None:
                    return full
                last_error = "длинный ответ дочитать не удалось"
                continue

            _, data = assemble_iso_tp(lines)
            if not data:
                last_error = "ответ не разобран"
                time.sleep(0.2)
                continue

            if data[0] != 0x7F:
                return data

            nrc = data[2] if len(data) > 2 else 0
            if nrc in (0x21, 0x78):
                # Блок занят или просит подождать — пауза и повтор
                time.sleep(0.4 + 0.2 * attempt)
                last_error = f"отказ блока: {NRC_TEXT.get(nrc)}"
                continue

            text = NRC_TEXT.get(nrc, f"код 0x{nrc:02X}")
            raise UdsError(f"отказ блока: {text}")

        raise UdsError(last_error)

    # -- сервисы ------------------------------------------------------------
    def tester_present(self, attempts=3):
        """
        Проверка связи. Запоминает адрес, с которого блок отвечает.
        Повторяет попытку: блоки кузовной электроники нередко
        не отвечают на первое обращение после пробуждения.
        """
        for attempt in range(attempts):
            if self._ping():
                return True
            time.sleep(0.3)
        return False

    def _ping(self):
        try:
            self.elm.set_header(self.tx_id)
            lines = self.elm.cmd("3E00", read_timeout=0.8)
            if is_negative(lines):
                return False
            source, data = assemble_iso_tp(lines)
            if not data:
                return False
            if source is not None:
                self.rx_id = source
                # Адаптер сам ведёт длинный обмен только там, где адрес
                # ответа равен адресу запроса плюс восемь. У блоков кузова
                # смещение другое — им сразу нужен ручной режим, иначе
                # первая же неудачная попытка оставит блок занятым.
                self.needs_manual = (source != self.tx_id + 8)
            return True
        except (UdsError, OSError):
            return False

    def start_session(self, level=0x03):
        """
        Открывает расширенную диагностическую сессию.
        Блоки кузовной электроники VAG часто не отдают ошибки
        в обычной сессии — сначала нужно попросить расширенную.
        """
        try:
            self.request(f"10{level:02X}", read_timeout=1.5)
            return True
        except (UdsError, OSError):
            return False

    def read_did(self, did, read_timeout=1.0):
        """
        Чтение идентификатора данных.

        Если обычный путь не сработал, пробуем ручную сборку кадров:
        на дешёвых адаптерах она оказывается надёжнее для блоков
        кузовной электроники.
        """
        data = self.request(f"22{did:04X}", read_timeout=read_timeout)
        if len(data) < 3 or data[0] != 0x62:
            raise UdsError("неожиданный формат ответа")
        return data[3:]

    def read_dtcs_raw(self, status_mask=0xFF):
        """Возвращает сырые байты ответа на запрос ошибок — для проверки разбора."""
        return self.request(f"1902{status_mask:02X}", read_timeout=2.5)

    def read_dtcs_kwp(self):
        """
        Запасной вариант: чтение ошибок по KWP2000, сервис 0x18.
        Формат записи здесь другой — два байта кода и байт состояния.
        """
        data = self.request("1802FF00", read_timeout=2.5)
        if len(data) < 2 or data[0] != 0x58:
            raise UdsError("неожиданный формат ответа KWP")
        count = data[1]
        records = data[2:]
        result = []
        for offset in range(0, min(len(records) - 2, count * 3), 3):
            code = records[offset:offset + 2]
            status = records[offset + 2]
            result.append((bytes(code) + b"\x00", status))
        return result

    def read_dtcs(self, status_mask=0xFF):
        """Чтение ошибок. Возвращает список кортежей (код, байт статуса)."""
        data = self.request(f"1902{status_mask:02X}", read_timeout=2.5)
        if len(data) < 3 or data[0] != 0x59:
            raise UdsError("неожиданный формат ответа на запрос ошибок")
        records = data[3:]
        result = []
        for offset in range(0, len(records) - 3, 4):
            code = records[offset:offset + 3]
            status = records[offset + 3]
            result.append((code, status))
        return result

    def clear_dtcs(self):
        """Стирание памяти ошибок. Единственная операция записи в программе."""
        self.request("14FFFFFF", read_timeout=3.0)
        return True


# ---------------------------------------------------------------------------
# Расшифровка ошибок
# ---------------------------------------------------------------------------
# Расшифровка байта типа отказа по ISO 14229-1.
# Именно он говорит, что искать: обрыв, замыкание или пропадание связи.
FAILURE_TYPE = {
    0x00: "тип не указан",
    0x11: "замыкание цепи на массу",
    0x12: "замыкание цепи на плюс",
    0x13: "обрыв цепи",
    0x14: "обрыв или замыкание на массу",
    0x16: "напряжение ниже порога",
    0x17: "напряжение выше порога",
    0x1A: "сопротивление ниже нормы",
    0x1B: "сопротивление выше нормы",
    0x21: "сигнал ниже минимума",
    0x22: "сигнал выше максимума",
    0x29: "сигнал недостоверен",
    0x2F: "сигнал нестабилен",
    0x62: "несовпадение сигналов",
    0x81: "неверные данные",
    0x87: "нет сообщений от узла",
    0x92: "неправильная работа узла",
}


def format_dtc(code_bytes):
    """
    Переводит три байта кода в привычный вид вида B310A-13.
    Первые два байта дают номер ошибки, третий — тип отказа.
    """
    if len(code_bytes) != 3:
        return "??"
    first = code_bytes[0]
    letter = "PCBU"[(first >> 6) & 0x03]
    digit = (first >> 4) & 0x03
    number = f"{letter}{digit}{first & 0x0F:X}{code_bytes[1]:02X}"
    return f"{number}-{code_bytes[2]:02X}"


def failure_text(code_bytes):
    """Словесное описание типа отказа из третьего байта кода."""
    if len(code_bytes) != 3:
        return "неизвестно"
    return FAILURE_TYPE.get(code_bytes[2], f"код 0x{code_bytes[2]:02X}")


def describe_status(status):
    """Расшифровывает байт состояния ошибки по стандарту UDS."""
    marks = []
    if status & 0x01:
        marks.append("активна сейчас")
    if status & 0x02:
        marks.append("сбой в этом цикле")
    if status & 0x08:
        marks.append("подтверждена")
    if status & 0x10:
        marks.append("не проверялась в этом цикле")
    if status & 0x20:
        marks.append("была ранее")
    return ", ".join(marks) if marks else "нет признаков"


def hint_for_dtc(text, status):
    """
    Подсказка по характеру дефекта. Точное описание кода даёт только
    база производителя, поэтому здесь оценивается тип и состояние.
    """
    if status & 0x01:
        state = "Дефект присутствует прямо сейчас — можно искать замерами."
    elif status & 0x20 and not status & 0x01:
        state = "Дефект плавающий: был, сейчас не проявляется. Это и есть плохой контакт."
    else:
        state = "Состояние неопределённое, повторить после сброса и поездки задним ходом."
    return state


# ---------------------------------------------------------------------------
# Журнал в CSV
# ---------------------------------------------------------------------------
RUN_STAMP = datetime.now().strftime("%d.%m.%Y %H:%M:%S")


class CsvLog:
    """
    Отчёт в CSV. В каждую строку добавляются версия программы и время
    запуска — чтобы всегда было видно, какой сборкой и когда получен файл.
    """

    def __init__(self, path, fieldnames):
        self.path = path
        self.fieldnames = list(fieldnames) + ["версия", "запуск"]
        self.handle = open(path, "w", newline="", encoding="utf-8-sig")
        self.writer = csv.DictWriter(self.handle, fieldnames=self.fieldnames,
                                     delimiter=";")
        self.writer.writeheader()

    def add(self, **row):
        row["версия"] = VERSION
        row["запуск"] = RUN_STAMP
        self.writer.writerow(row)
        self.handle.flush()

    def close(self):
        try:
            self.handle.close()
        except Exception:
            pass


def timestamp():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


REPORT_FILES = ("scan_result.csv", "modules_result.csv", "probe_result.csv",
                "full_report.csv", "dtc_result.csv", "info_result.csv",
                "dids_result.csv", "watch_log.csv", "shake_log.csv",
                "elm_raw.log", "REPORT_TO_SEND.txt", "target_result.csv",
                "monitor_log.csv")


def clear_old_reports():
    """
    Удаляет отчёты предыдущих запусков.

    Иначе в папке остаются файлы от прошлых версий программы, их легко
    принять за свежие и сделать неверный вывод.
    """
    removed = []
    for name in REPORT_FILES:
        if os.path.exists(name):
            try:
                os.remove(name)
                removed.append(name)
            except OSError:
                pass
    return removed


def build_bundle(elm, results=None, path="REPORT_TO_SEND.txt"):
    """
    Складывает всё нужное для разбора в ОДИН файл.

    Отдельные CSV и журнал легко потерять или перепутать с файлами
    прошлых запусков. Один файл с версией, временем, итогами и полным
    обменом такой возможности не оставляет.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("=" * 70 + "\n")
            fh.write(f"ОТЧЁТ ДЛЯ ОТПРАВКИ\n")
            fh.write(f"версия программы : {VERSION}\n")
            fh.write(f"время запуска    : {RUN_STAMP}\n")
            fh.write("=" * 70 + "\n\n")

            if results:
                fh.write("ИТОГИ ПО БЛОКАМ\n")
                fh.write("-" * 70 + "\n")
                for item in results:
                    rx = f"0x{item['rx']:03X}" if item.get("rx") else "—"
                    fh.write(f"0x{item['id']:03X}  имя: {item['name'] or '—'}  "
                             f"номер: {item['part'] or '—'}  "
                             f"ответ с: {rx}  "
                             f"ошибок: {len(item['faults'])}  "
                             f"способ: {item.get('mf') or 'обычный'}  "
                             f"{item['note']}\n")
                fh.write("\n")

            unsupported = getattr(elm, "unsupported", [])
            if unsupported:
                fh.write("КОМАНДЫ, НЕ ПОНЯТЫЕ АДАПТЕРОМ\n")
                fh.write("-" * 70 + "\n")
                for command in sorted(set(unsupported)):
                    fh.write(f"{command}\n")
                fh.write("\n")

            fh.write("ПОЛНЫЙ ОБМЕН С АДАПТЕРОМ\n")
            fh.write("-" * 70 + "\n")
            for request, raw in elm.raw_log:
                fh.write(f">> {request}\n<< {raw!r}\n")
        return path
    except OSError:
        return None


def dump_raw_log(elm, path="elm_raw.log"):
    """
    Сохраняет весь обмен с адаптером.

    Нужен, когда результат выглядит неправдоподобно: по сырому обмену
    видно, что именно ответил адаптер, а не что из этого поняла программа.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"версия программы {VERSION}\n")
            fh.write(f"дата {datetime.now():%d.%m.%Y %H:%M:%S}\n\n")
            for request, raw in elm.raw_log:
                fh.write(f">> {request}\n<< {raw!r}\n")
        return path
    except OSError:
        return None
