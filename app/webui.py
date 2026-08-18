#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
webui.py — графический интерфейс диагностики через браузер.

Почему браузер, а не окно на Tkinter: пакет поставляется с переносимым
Python, в состав которого Tkinter не входит. Локальный веб-сервер решает
это без единой внешней библиотеки и заодно даёт нормальный современный
интерфейс. Интернет не нужен — страница отдаётся с этого же компьютера.

Запуск:  python webui.py       (или через START_GUI.bat)
"""

import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from pdc_core import (VERSION, DEFAULT_HOST, DEFAULT_PORT, CsvLog, Elm327,  # noqa: E402
                      UdsClient, UdsError, assemble_iso_tp, declared_length,
                      describe_status, failure_text, format_dtc, is_negative,
                      is_truncated, partial_payload)
import pdc_obd     # noqa: E402
import pdc_codes   # noqa: E402
import pdc_serial  # noqa: E402
from pdc_slcan import SlcanAdapter  # noqa: E402

HOST_UI = "127.0.0.1"
PORT_UI = 8765

STATUS_BITS = {
    0xFF: "все ошибки",
    0x01: "активна сейчас",
    0x02: "сбой в этом цикле",
    0x04: "ожидает проверки",
    0x08: "подтверждена",
    0x10: "не проверялась",
    0x20: "была ранее",
}

# Известные адреса блоков VAG. Быстрый список для тех, кто не хочет
# ждать полного перебора всех 256 адресов.
KNOWN_IDS = [0x700, 0x703, 0x70A, 0x711, 0x712, 0x713, 0x714, 0x715,
             0x716, 0x717, 0x71E, 0x744, 0x746, 0x74F, 0x754, 0x767,
             0x76E, 0x773, 0x776, 0x77E, 0x7E0, 0x7E1, 0x7F1]


class Engine:
    """Выполняет задачи по очереди. Адаптер один, параллелить нечего."""

    def __init__(self):
        self.lock = threading.Lock()
        self.elm = None
        self.host = DEFAULT_HOST
        self.port = DEFAULT_PORT
        self.busy = False
        self.task = ""
        self.progress = 0
        self.message = "Не подключено"
        self.adapter = {}
        self.modules = []
        self.details = {}
        self.monitor_samples = []
        self.monitor_running = False
        self.log_lines = []
        self.obd = {}                 # коды двигателя, готовность, VIN
        self.live = {"running": False, "pids": [], "samples": []}
        self.wizard = {"rows": [], "active": False, "module": None}
        self.vehicle = {}
        self.snapshots = []
        self.comparison = {}
        self.battery = {"running": False, "samples": []}
        self.ports = []

    # -- служебное ---------------------------------------------------------
    def say(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.log_lines.append(f"{stamp}  {text}")
            self.log_lines = self.log_lines[-400:]
            self.message = text

    def refresh_ports(self):
        """Обновляет список доступных COM-портов."""
        try:
            found = pdc_serial.list_ports()
        except Exception:                                # noqa: BLE001
            found = []
        with self.lock:
            self.ports = found
        self.say(f"Найдено портов: {len(found)}")

    def snapshot(self):
        with self.lock:
            return {
                "version": VERSION,
                "busy": self.busy,
                "task": self.task,
                "progress": self.progress,
                "message": self.message,
                "adapter": self.adapter,
                "connected": self.elm is not None,
                "modules": self.modules,
                "details": self.details,
                "monitor": self.monitor_samples[-60:],
                "monitorRunning": self.monitor_running,
                "log": self.log_lines[-60:],
                "host": self.host,
                "port": self.port,
                "obd": self.obd,
                "live": {"running": self.live["running"],
                         "pids": self.live["pids"],
                         "samples": self.live["samples"][-40:]},
                "wizard": self.wizard,
                "vehicle": self.vehicle,
                "snapshots": self.snapshots,
                "comparison": self.comparison,
                "battery": self.battery,
                "ports": self.ports,
            }

    def start(self, name, target, *args):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            self.task = name
            self.progress = 0

        def runner():
            try:
                target(*args)
            except Exception as exc:                    # noqa: BLE001
                self.say(f"Ошибка: {exc}")
            finally:
                with self.lock:
                    self.busy = False
                    self.task = ""
                    self.progress = 100

        threading.Thread(target=runner, daemon=True).start()
        return True

    # -- подключение -------------------------------------------------------
    def connect(self, host, port, kind="wifi", com="", bitrate=500):
        """
        Подключается одним из трёх способов: по сети, через COM-порт
        или через USB-CAN адаптер. Для остальной программы разницы нет.
        """
        self.host, self.port = host, int(port)
        if self.elm:
            try:
                self.elm.close()
            except Exception:                            # noqa: BLE001
                pass
            self.elm = None

        if kind == "slcan":
            self.say(f"Подключение к USB-CAN на {com}, {bitrate} кбит/с")
            adapter = SlcanAdapter(com, bitrate=int(bitrate))
            try:
                adapter.connect()
            except OSError as exc:
                self.say(f"Не удалось открыть порт: {exc}")
                return
            version, voltage = adapter.identify()
            adapter.accept_all()
            with self.lock:
                self.elm = adapter
                self.adapter = {"version": version, "voltage": voltage,
                                "warn": "", "kind": "USB-CAN"}
            self.say(f"USB-CAN подключён: {version}")
            return

        if kind == "serial":
            self.say(f"Подключение через порт {com}")
            elm = Elm327(host, int(port))
            try:
                elm.sock = pdc_serial.SerialChannel(com, baudrate=38400,
                                                    timeout=6.0)
            except OSError as exc:
                self.say(f"Не удалось открыть порт: {exc}")
                return
            time.sleep(0.3)
        else:
            self.say(f"Подключение к адаптеру {host}:{port}...")
            elm = Elm327(host, int(port))
            try:
                elm.connect()
            except OSError as exc:
                self.say(f"Не удалось подключиться: {exc}")
                with self.lock:
                    self.adapter = {}
                return
        elm.init()
        version, voltage = elm.identify()
        elm.accept_all()

        warn = ""
        try:
            value = float(voltage.upper().replace("V", "").strip())
            if value < 11.5:
                warn = "напряжение низкое, возможны ложные ошибки"
            elif value > 13.3:
                warn = "двигатель запущен, лучше заглушить"
        except ValueError:
            pass

        with self.lock:
            self.elm = elm
            self.adapter = {"version": version, "voltage": voltage,
                            "warn": warn,
                            "kind": "COM-порт" if kind == "serial" else "Wi-Fi"}
        self.say(f"Адаптер {version}, бортсеть {voltage}")

    def disconnect(self):
        with self.lock:
            elm, self.elm = self.elm, None
            self.adapter = {}
        if elm:
            try:
                elm.close()
            except Exception:                            # noqa: BLE001
                pass
        self.say("Отключено")

    # -- поиск блоков ------------------------------------------------------
    def scan(self, full):
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        ids = list(range(0x700, 0x800)) if full else KNOWN_IDS
        self.say(f"Поиск блоков: проверяется адресов {len(ids)}")
        self.elm.accept_all()

        found = []
        for index, can_id in enumerate(ids, start=1):
            self.elm.set_header(can_id)
            lines = self.elm.cmd("3E00", read_timeout=0.6)
            if not is_negative(lines):
                source, data = assemble_iso_tp(lines)
                if data:
                    found.append({"id": can_id,
                                  "hex": f"0x{can_id:03X}",
                                  "rx": f"0x{source:03X}" if source else "",
                                  "name": "", "part": "",
                                  "faults": None, "firstCode": ""})
                    self.say(f"Найден блок 0x{can_id:03X}")
            with self.lock:
                self.progress = int(index * 100 / len(ids))

        with self.lock:
            self.modules = found
        self.say(f"Найдено блоков: {len(found)}")

    # -- краткие данные по всем блокам ------------------------------------
    def brief(self):
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        with self.lock:
            modules = list(self.modules)
        if not modules:
            self.say("Сначала выполните поиск блоков")
            return

        self.say("Сбор кратких данных по всем блокам")
        for index, item in enumerate(modules, start=1):
            can_id = item["id"]
            uds = UdsClient(self.elm, can_id)
            self.elm.accept_all()
            if not uds.tester_present():
                item["name"] = "нет ответа"
                self._push_modules(modules)
                continue
            item["rx"] = f"0x{uds.rx_id:03X}" if uds.rx_id else ""
            uds.start_session(0x03)

            for did, key in ((0xF197, "name"), (0xF187, "part")):
                try:
                    value = uds.read_did(did, read_timeout=1.2)
                    item[key] = "".join(chr(b) if 32 <= b < 127 else ""
                                        for b in value).strip()
                except (UdsError, OSError):
                    pass

            count, code, failure = self._fault_brief(can_id, 0xFF)
            item["faults"] = count
            item["firstCode"] = f"{code} {failure}".strip()

            self._push_modules(modules)
            with self.lock:
                self.progress = int(index * 100 / len(modules))
            time.sleep(0.2)
        self.say("Краткие данные собраны")

    def _push_modules(self, modules):
        with self.lock:
            self.modules = list(modules)

    def _fault_brief(self, can_id, mask):
        """Число ошибок и первый код. Работает даже на обрезанном ответе."""
        self.elm.set_header(can_id)
        answer = self.elm.cmd(f"1902{mask:02X}", read_timeout=2.5)
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

    # -- подробности по блоку ---------------------------------------------
    def detail(self, can_id):
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.say(f"Разбор ошибок блока 0x{can_id:03X}")
        self.elm.accept_all()
        uds = UdsClient(self.elm, can_id)
        if not uds.tester_present():
            self.say("Блок не отвечает")
            return
        uds.start_session(0x03)

        rows = []
        for step, (mask, title) in enumerate(STATUS_BITS.items(), start=1):
            count, code, failure = self._fault_brief(can_id, mask)
            rows.append({"mask": f"0x{mask:02X}", "title": title,
                         "count": count, "code": code, "failure": failure})
            with self.lock:
                self.progress = int(step * 100 / len(STATUS_BITS))
            time.sleep(1.5)

        with self.lock:
            self.details[f"0x{can_id:03X}"] = {
                "rows": rows,
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        self.say("Разбор завершён")

    # -- стирание ----------------------------------------------------------
    def clear(self, can_id):
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.say(f"Стирание памяти ошибок блока 0x{can_id:03X}")
        self.elm.accept_all()
        uds = UdsClient(self.elm, can_id)
        if not uds.tester_present():
            self.say("Блок не отвечает")
            return
        uds.start_session(0x03)
        time.sleep(0.3)
        answer = self.elm.cmd("14FFFFFF", read_timeout=3.0)
        text = " ".join(answer)
        if is_negative(answer) or "7F" in text:
            self.say("Первый способ не прошёл, пробую сокращённый запрос")
            time.sleep(0.5)
            answer = self.elm.cmd("04", read_timeout=3.0)
            text = " ".join(answer)
        self.say(f"Ответ блока: {text or 'нет ответа'}")

    # -- монитор -----------------------------------------------------------
    def monitor_start(self, can_id):
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        with self.lock:
            self.monitor_running = True
            self.monitor_samples = []
        self.say("Монитор запущен")

        self.elm.accept_all()
        uds = UdsClient(self.elm, can_id)
        if not uds.tester_present():
            self.say("Блок не отвечает")
            with self.lock:
                self.monitor_running = False
            return

        previous = None
        log = CsvLog("monitor_log.csv",
                     ["время", "активных", "всего", "код", "событие"])
        try:
            while True:
                with self.lock:
                    if not self.monitor_running:
                        break
                active, code, failure = self._fault_brief(can_id, 0x01)
                time.sleep(1.2)
                total, _, _ = self._fault_brief(can_id, 0xFF)

                event = ""
                if active is not None and previous is not None and active != previous:
                    event = f"изменилось на {active - previous:+d}"
                if active is not None:
                    previous = active

                sample = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "active": active, "total": total,
                    "code": f"{code} {failure}".strip(), "event": event,
                }
                with self.lock:
                    self.monitor_samples.append(sample)
                    self.monitor_samples = self.monitor_samples[-200:]
                log.add(время=sample["time"], активных=active, всего=total,
                        код=sample["code"], событие=event)
                time.sleep(1.3)
        finally:
            log.close()
            with self.lock:
                self.monitor_running = False
            self.say("Монитор остановлен")

    def monitor_stop(self):
        with self.lock:
            self.monitor_running = False


    # -- универсальная диагностика OBD-II ---------------------------------
    def obd_read(self):
        """Коды двигателя, готовность систем, VIN и стоп-кадр."""
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.elm.accept_all()

        self.say("Чтение VIN")
        vin = pdc_obd.read_vin(self.elm)
        with self.lock:
            self.progress = 15

        self.say("Чтение кодов двигателя")
        stored = pdc_obd.read_dtcs(self.elm, 0x03)
        with self.lock:
            self.progress = 40
        pending = pdc_obd.read_dtcs(self.elm, 0x07)
        with self.lock:
            self.progress = 60
        permanent = pdc_obd.read_dtcs(self.elm, 0x0A)
        with self.lock:
            self.progress = 75

        self.say("Проверка готовности систем")
        readiness = pdc_obd.read_readiness(self.elm)
        with self.lock:
            self.progress = 90

        freeze = pdc_obd.read_freeze_frame(self.elm) if stored else []

        with self.lock:
            self.obd = {"vin": vin,
                        "stored": [{"code": c, "text": pdc_codes.describe(c),
                                    "serious": pdc_codes.is_serious(c)}
                                   for c in stored],
                        "pending": [{"code": c, "text": pdc_codes.describe(c),
                                     "serious": pdc_codes.is_serious(c)}
                                    for c in pending],
                        "permanent": [{"code": c, "text": pdc_codes.describe(c),
                                       "serious": pdc_codes.is_serious(c)}
                                      for c in permanent],
                        "readiness": readiness,
                        "freeze": freeze,
                        "time": datetime.now().strftime("%H:%M:%S")}
            if vin:
                self.vehicle["vin"] = vin
        total = len(stored) + len(pending) + len(permanent)
        self.say(f"Готово. Кодов двигателя: {total}")

    def obd_clear(self):
        """Стирание кодов двигателя и гашение лампы неисправности."""
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.elm.accept_all()
        ok = pdc_obd.clear_dtcs(self.elm)
        self.say("Коды двигателя стёрты" if ok else "Блок отказал в стирании")

    # -- живые параметры ---------------------------------------------------
    def live_start(self, pids):
        """Циклический опрос выбранных параметров."""
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.elm.accept_all()

        if not pids:
            self.say("Определяю, какие параметры поддерживает двигатель")
            supported = pdc_obd.supported_pids(self.elm)
            pids = [p for p in pdc_obd.PIDS if p in supported] or \
                   [0x05, 0x0C, 0x0D, 0x11, 0x42]
            self.say(f"Доступно параметров: {len(pids)}")

        with self.lock:
            self.live = {"running": True,
                         "pids": [{"pid": p, "title": pdc_obd.PIDS[p][0],
                                   "unit": pdc_obd.PIDS[p][1]} for p in pids],
                         "samples": []}
        log = CsvLog("live_log.csv", ["время"] + [pdc_obd.PIDS[p][0] for p in pids])
        try:
            while True:
                with self.lock:
                    if not self.live["running"]:
                        break
                row = {"время": datetime.now().strftime("%H:%M:%S")}
                values = {}
                for pid in pids:
                    result = pdc_obd.read_pid(self.elm, pid)
                    if result:
                        title, value, unit = result
                        values[pid] = value
                        row[title] = value
                    else:
                        values[pid] = None
                        row[pdc_obd.PIDS[pid][0]] = ""
                sample = {"time": row["время"], "values":
                          {str(k): v for k, v in values.items()}}
                with self.lock:
                    self.live["samples"].append(sample)
                    self.live["samples"] = self.live["samples"][-200:]
                log.add(**row)
                time.sleep(0.4)
        finally:
            log.close()
            with self.lock:
                self.live["running"] = False
            self.say("Опрос параметров остановлен")

    def live_stop(self):
        with self.lock:
            self.live["running"] = False

    # -- мастер поиска датчика --------------------------------------------
    def wizard_sample(self, can_id, label):
        """
        Снимает показание счётчика ошибок и подписывает его.

        Так строится карта «датчик — код»: отключаете один датчик,
        подписываете шаг его именем, программа фиксирует, какая ошибка
        появилась. После обхода всех датчиков видно, какой код чей.
        """
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.elm.accept_all()
        active, code, failure = self._fault_brief(can_id, 0x01)
        time.sleep(1.0)
        total, _, _ = self._fault_brief(can_id, 0xFF)

        with self.lock:
            previous = self.wizard["rows"][-1]["active"] if self.wizard["rows"] else None
            delta = ""
            if previous is not None and active is not None:
                delta = f"{active - previous:+d}"
            self.wizard["rows"].append({
                "label": label or f"шаг {len(self.wizard['rows']) + 1}",
                "time": datetime.now().strftime("%H:%M:%S"),
                "active": active, "total": total, "delta": delta,
                "code": f"{code} {failure}".strip(),
            })
            self.wizard["module"] = f"0x{can_id:03X}"
            self.wizard["active"] = True
        self.say(f"Шаг записан: {label}, активных ошибок {active}")

    def wizard_reset(self):
        with self.lock:
            self.wizard = {"rows": [], "active": False, "module": None}
        self.say("Мастер сброшен")

    # -- полное сканирование одной кнопкой --------------------------------
    def autoscan(self):
        """Поиск блоков, сбор данных, разбор ошибок и сохранение отчёта."""
        self.scan(False)
        self.brief()
        with self.lock:
            modules = [m for m in self.modules if m.get("faults")]
        for item in modules:
            self.detail(item["id"])
        self.report()
        self.say("Полное сканирование завершено")

    # -- обслуживание адаптера --------------------------------------------
    def adapter_reset(self):
        """Программный перезапуск адаптера. Помогает, когда он завис."""
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.say("Перезапуск адаптера")
        self.elm.cmd("ATZ", read_timeout=4.0)
        time.sleep(0.8)
        self.elm.init()
        self.elm.accept_all()
        version, voltage = self.elm.identify()
        with self.lock:
            self.adapter = {"version": version, "voltage": voltage, "warn": ""}
        self.say(f"Адаптер перезапущен: {version}, {voltage}")

    def protocol_info(self):
        """Показывает, какой протокол связи выбран адаптером."""
        if not self.elm:
            return
        described = " ".join(self.elm.cmd("ATDP"))
        self.say(f"Протокол связи: {described}")

    # -- отчёт в виде страницы --------------------------------------------
    def report_html(self):
        """Отчёт в HTML: удобно открыть, распечатать или переслать."""
        with self.lock:
            modules = list(self.modules)
            details = dict(self.details)
            adapter = dict(self.adapter)
            obd = dict(self.obd)
            wizard = dict(self.wizard)

        def table(rows, headers):
            head = "".join(f"<th>{h}</th>" for h in headers)
            body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) +
                           "</tr>" for row in rows)
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

        parts = [f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>Отчёт диагностики</title><style>
body{{font-family:Segoe UI,sans-serif;margin:28px;color:#1c1c1c;font-size:14px}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:26px;
border-bottom:2px solid #0f5a45;padding-bottom:4px;color:#0f5a45}}
table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}}
th,td{{border:1px solid #c8c6bc;padding:5px 8px;text-align:left}}
th{{background:#eceae2}} .muted{{color:#777}}
</style></head><body>
<h1>Отчёт диагностики</h1>
<p class="muted">Программа версии {VERSION} ·
{datetime.now():%d.%m.%Y %H:%M} ·
адаптер {adapter.get('version', '—')}, бортсеть {adapter.get('voltage', '—')}</p>"""]

        if obd:
            parts.append("<h2>Двигатель, стандарт OBD-II</h2>")
            parts.append(f"<p>VIN: <b>{obd.get('vin') or '—'}</b></p>")
            for key, title in (("stored", "Сохранённые коды"),
                               ("pending", "Неподтверждённые коды"),
                               ("permanent", "Постоянные коды")):
                codes = obd.get(key) or []
                parts.append(f"<p>{title}: " +
                             (", ".join(codes) if codes else
                              '<span class="muted">нет</span>') + "</p>")
            readiness = obd.get("readiness")
            if readiness:
                parts.append("<p>Лампа неисправности: " +
                             ("<b>горит</b>" if readiness["mil"] else "погашена") + "</p>")
                parts.append(table([[c["title"], c["state"]]
                                    for c in readiness["checks"]],
                                   ["Система", "Состояние"]))

        if modules:
            parts.append("<h2>Блоки управления</h2>")
            parts.append(table(
                [[m["hex"], m["rx"] or "—", m["name"] or "—", m["part"] or "—",
                  "—" if m["faults"] is None else m["faults"], m["firstCode"] or "—"]
                 for m in modules],
                ["Адрес", "Ответ с", "Имя", "Номер запчасти", "Ошибок", "Первый код"]))

        for key, block in details.items():
            parts.append(f"<h2>Разбор ошибок блока {key}</h2>")
            parts.append(table(
                [[r["title"], "—" if r["count"] is None else r["count"],
                  f"{r['code']} {r['failure']}".strip() or "—"]
                 for r in block["rows"]],
                ["Признак", "Количество", "Первый код"]))

        if wizard.get("rows"):
            parts.append("<h2>Мастер поиска датчика</h2>")
            parts.append(table(
                [[r["label"], r["time"], r["active"], r["delta"], r["code"]]
                 for r in wizard["rows"]],
                ["Шаг", "Время", "Активных", "Изменение", "Код"]))

        parts.append("</body></html>")
        path = "otchet.html"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
        self.say(f"Отчёт сохранён: {os.path.abspath(path)}")


    # -- снимки и сравнение -------------------------------------------------
    def snapshot_save(self, title):
        """
        Сохраняет текущую картину ошибок как снимок.

        Нужно для главного вопроса ремонта: что изменилось после того,
        как я что-то починил. Снимок до и снимок после дают точный ответ,
        вместо попыток вспомнить, сколько ошибок было вчера.
        """
        os.makedirs("history", exist_ok=True)
        with self.lock:
            modules = list(self.modules)
            obd = dict(self.obd)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        record = {
            "title": title or stamp,
            "time": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "modules": [{"hex": m["hex"], "name": m["name"],
                         "faults": m["faults"], "firstCode": m["firstCode"]}
                        for m in modules],
            "engine": {key: [item["code"] if isinstance(item, dict) else item
                             for item in obd.get(key, [])]
                       for key in ("stored", "pending", "permanent")},
        }
        with open(os.path.join("history", f"{stamp}.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2)
        self._reload_snapshots()
        self.say(f"Снимок сохранён: {record['title']}")

    def _reload_snapshots(self):
        items = []
        try:
            for name in sorted(os.listdir("history"), reverse=True):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join("history", name), encoding="utf-8") as fh:
                    data = json.load(fh)
                data["file"] = name
                items.append(data)
        except (OSError, ValueError):
            pass
        with self.lock:
            self.snapshots = items[:30]

    def snapshot_compare(self, first, second):
        """Сравнивает два снимка и показывает, что ушло, осталось и добавилось."""
        def load(name):
            with open(os.path.join("history", name), encoding="utf-8") as fh:
                return json.load(fh)

        try:
            before, after = load(first), load(second)
        except (OSError, ValueError) as exc:
            self.say(f"Не удалось прочитать снимки: {exc}")
            return

        def engine_codes(record):
            block = record.get("engine", {})
            return set(block.get("stored", []) + block.get("pending", [])
                       + block.get("permanent", []))

        gone = sorted(engine_codes(before) - engine_codes(after))
        fresh = sorted(engine_codes(after) - engine_codes(before))
        stayed = sorted(engine_codes(before) & engine_codes(after))

        rows = []
        by_hex = {m["hex"]: m for m in after.get("modules", [])}
        for module in before.get("modules", []):
            other = by_hex.get(module["hex"])
            was = module.get("faults")
            now = other.get("faults") if other else None
            if was == now:
                verdict = "без изменений"
            elif was is None or now is None:
                verdict = "нет данных"
            elif now < was:
                verdict = f"стало меньше на {was - now}"
            else:
                verdict = f"стало больше на {now - was}"
            rows.append({"hex": module["hex"], "name": module.get("name", ""),
                         "before": was, "after": now, "verdict": verdict})

        with self.lock:
            self.comparison = {
                "before": before.get("title"), "after": after.get("title"),
                "gone": [{"code": c, "text": pdc_codes.describe(c)} for c in gone],
                "fresh": [{"code": c, "text": pdc_codes.describe(c)} for c in fresh],
                "stayed": [{"code": c, "text": pdc_codes.describe(c)} for c in stayed],
                "modules": rows,
            }
        self.say(f"Сравнение готово: ушло {len(gone)}, "
                 f"осталось {len(stayed)}, появилось {len(fresh)}")

    # -- тест аккумулятора и генератора ------------------------------------
    def battery_test(self, seconds):
        """
        Пишет напряжение бортсети часто и подробно.

        Так проверяют аккумулятор и генератор без отдельного прибора:
        глубина просадки при пуске говорит о состоянии батареи,
        а напряжение на работающем двигателе — о генераторе.
        """
        if not self.elm:
            self.say("Сначала подключитесь к адаптеру")
            return
        self.say(f"Замер напряжения, {seconds} секунд. "
                 f"Можно запускать двигатель.")
        values = []
        log = CsvLog("battery_log.csv", ["время", "секунда", "напряжение"])
        started = time.time()
        try:
            while time.time() - started < seconds:
                answer = " ".join(self.elm.cmd("ATRV", read_timeout=1.0))
                try:
                    value = float(answer.upper().replace("V", "").strip())
                except ValueError:
                    continue
                elapsed = round(time.time() - started, 2)
                values.append({"t": elapsed, "v": value})
                log.add(время=datetime.now().strftime("%H:%M:%S"),
                        секунда=elapsed, напряжение=value)
                with self.lock:
                    self.battery = {"running": True, "samples": values[-300:]}
                    self.progress = int((time.time() - started) * 100 / seconds)
                time.sleep(0.15)
        finally:
            log.close()

        if not values:
            self.say("Не удалось получить напряжение")
            return

        readings = [v["v"] for v in values]
        low, high = min(readings), max(readings)
        verdict = []
        if low < 9.6:
            verdict.append("Просадка при пуске ниже 9,6 В — аккумулятор слаб "
                           "либо велико сопротивление в цепи стартера")
        elif low < 10.5:
            verdict.append("Просадка при пуске на границе нормы, "
                           "аккумулятор стоит проверить нагрузочной вилкой")
        if high > 13.2:
            if high > 15.0:
                verdict.append("Напряжение выше 15 В — неисправен регулятор "
                               "генератора, аккумулятор будет выкипать")
            elif high < 13.6:
                verdict.append("Зарядное напряжение занижено, "
                               "аккумулятор будет недозаряжаться")
            else:
                verdict.append("Зарядное напряжение в норме, генератор работает")
        else:
            verdict.append("Признаков зарядки не видно: либо двигатель "
                           "не запускался, либо генератор не отдаёт ток")

        with self.lock:
            self.battery = {"running": False, "samples": values[-300:],
                            "low": low, "high": high,
                            "verdict": verdict}
        self.say(f"Замер окончен: минимум {low} В, максимум {high} В")

    # -- отчёт -------------------------------------------------------------
    def report(self):
        path = "REPORT_TO_SEND.txt"
        with self.lock:
            modules = list(self.modules)
            details = dict(self.details)
            adapter = dict(self.adapter)
            samples = list(self.monitor_samples)
            raw = list(self.elm.raw_log) if self.elm else []
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("=" * 70 + "\n")
            fh.write("ОТЧЁТ ДЛЯ ОТПРАВКИ\n")
            fh.write(f"версия программы : {VERSION}\n")
            fh.write(f"время            : {datetime.now():%d.%m.%Y %H:%M:%S}\n")
            fh.write(f"адаптер          : {adapter.get('version', '—')}, "
                     f"бортсеть {adapter.get('voltage', '—')}\n")
            fh.write("=" * 70 + "\n\n")

            fh.write("БЛОКИ\n" + "-" * 70 + "\n")
            for item in modules:
                fh.write(f"{item['hex']}  ответ с {item['rx'] or '—'}  "
                         f"имя: {item['name'] or '—'}  "
                         f"номер: {item['part'] or '—'}  "
                         f"ошибок: {item['faults'] if item['faults'] is not None else '—'}  "
                         f"{item['firstCode']}\n")

            for key, block in details.items():
                fh.write(f"\nРАЗБОР ОШИБОК {key}\n" + "-" * 70 + "\n")
                for row in block["rows"]:
                    fh.write(f"{row['title']:<20} {str(row['count']):>4}  "
                             f"{row['code']} {row['failure']}\n")

            if samples:
                fh.write("\nМОНИТОР\n" + "-" * 70 + "\n")
                for sample in samples:
                    fh.write(f"{sample['time']}  активных {sample['active']}  "
                             f"всего {sample['total']}  {sample['code']}  "
                             f"{sample['event']}\n")

            fh.write("\nПОЛНЫЙ ОБМЕН С АДАПТЕРОМ\n" + "-" * 70 + "\n")
            for request, answer in raw:
                fh.write(f">> {request}\n<< {answer!r}\n")
        self.say(f"Отчёт сохранён: {os.path.abspath(path)}")


ENGINE = Engine()
ENGINE._reload_snapshots()


class Handler(BaseHTTPRequestHandler):
    """Отдаёт страницу и обслуживает запросы интерфейса."""

    def log_message(self, *args):
        pass                     # не засорять консоль

    def _send(self, code, body, content_type="application/json"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE, "ui.html"), encoding="utf-8") as fh:
                    return self._send(200, fh.read(), "text/html")
            except OSError:
                return self._send(500, "ui.html не найден", "text/plain")
        if path == "/api/state":
            return self._send(200, json.dumps(ENGINE.snapshot(), ensure_ascii=False))
        return self._send(404, "{}")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            data = {}

        actions = {
            "/api/connect": lambda: ENGINE.start(
                "Подключение", ENGINE.connect,
                data.get("host", DEFAULT_HOST), data.get("port", DEFAULT_PORT),
                data.get("kind", "wifi"), data.get("com", ""),
                data.get("bitrate", 500)),
            "/api/disconnect": lambda: ENGINE.start("Отключение", ENGINE.disconnect),
            "/api/scan": lambda: ENGINE.start(
                "Поиск блоков", ENGINE.scan, bool(data.get("full"))),
            "/api/brief": lambda: ENGINE.start("Сбор данных", ENGINE.brief),
            "/api/detail": lambda: ENGINE.start(
                "Разбор ошибок", ENGINE.detail, int(data.get("id", 0))),
            "/api/clear": lambda: ENGINE.start(
                "Стирание ошибок", ENGINE.clear, int(data.get("id", 0))),
            "/api/monitor/start": lambda: ENGINE.start(
                "Монитор", ENGINE.monitor_start, int(data.get("id", 0))),
            "/api/report": lambda: ENGINE.start("Отчёт", ENGINE.report),
            "/api/report/html": lambda: ENGINE.start(
                "Отчёт HTML", ENGINE.report_html),
            "/api/obd/read": lambda: ENGINE.start(
                "Диагностика двигателя", ENGINE.obd_read),
            "/api/obd/clear": lambda: ENGINE.start(
                "Стирание кодов двигателя", ENGINE.obd_clear),
            "/api/live/start": lambda: ENGINE.start(
                "Живые параметры", ENGINE.live_start,
                [int(p) for p in data.get("pids", [])]),
            "/api/wizard/sample": lambda: ENGINE.start(
                "Замер мастера", ENGINE.wizard_sample,
                int(data.get("id", 0)), str(data.get("label", ""))),
            "/api/wizard/reset": lambda: ENGINE.start(
                "Сброс мастера", ENGINE.wizard_reset),
            "/api/autoscan": lambda: ENGINE.start(
                "Полное сканирование", ENGINE.autoscan),
            "/api/adapter/reset": lambda: ENGINE.start(
                "Перезапуск адаптера", ENGINE.adapter_reset),
            "/api/ports": lambda: ENGINE.start(
                "Поиск портов", ENGINE.refresh_ports),
            "/api/snapshot/save": lambda: ENGINE.start(
                "Снимок", ENGINE.snapshot_save, str(data.get("title", ""))),
            "/api/snapshot/compare": lambda: ENGINE.start(
                "Сравнение", ENGINE.snapshot_compare,
                str(data.get("first", "")), str(data.get("second", ""))),
            "/api/battery": lambda: ENGINE.start(
                "Замер напряжения", ENGINE.battery_test,
                int(data.get("seconds", 20))),
        }

        if path == "/api/monitor/stop":
            ENGINE.monitor_stop()
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/live/stop":
            ENGINE.live_stop()
            return self._send(200, json.dumps({"ok": True}))

        action = actions.get(path)
        if not action:
            return self._send(404, "{}")
        accepted = action()
        return self._send(200, json.dumps({"ok": bool(accepted)}))


def main():
    server = ThreadingHTTPServer((HOST_UI, PORT_UI), Handler)
    url = f"http://{HOST_UI}:{PORT_UI}/"
    print("=" * 70)
    print(f"  ДИАГНОСТИКА VAG — версия {VERSION}")
    print("=" * 70)
    print(f"  Интерфейс открыт: {url}")
    print("  Это окно не закрывать — оно и есть программа.")
    print("  Для выхода закройте окно или нажмите Ctrl+C.")
    print("=" * 70)
    try:
        webbrowser.open(url)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nЗавершение работы.")


if __name__ == "__main__":
    main()
