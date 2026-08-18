#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdc_slcan.py — работа через USB-CAN адаптер по протоколу SLCAN.

Зачем это нужно. Прошивка ELM327 ведёт многокадровый обмен только
с парой адресов двигателя, зашитой в неё намертво. Блоки кузовной
электроники отвечают с другим смещением, и адаптер не запрашивает
у них продолжение длинного ответа — читается только первый кадр.
Обойти это командами невозможно, проверено.

USB-CAN адаптер отдаёт сырые кадры шины и ничего не решает за нас.
Сборку ISO-TP и отправку кадра разрешения программа делает сама,
и длинные списки ошибок читаются целиком.

Подходят адаптеры CANable, CANtact и совместимые: они принимают
текстовые команды по последовательному порту.

Класс SlcanAdapter намеренно повторяет интерфейс класса Elm327,
поэтому остальная программа работает с ним без единой правки.
"""

import time

from pdc_serial import SerialChannel

# Скорости шины. Комфортная шина VAG обычно 500 кбит/с, реже 100.
BITRATES = {
    10: "S0", 20: "S1", 50: "S2", 100: "S3", 125: "S4",
    250: "S5", 500: "S6", 800: "S7", 1000: "S8",
}


class SlcanAdapter:
    """USB-CAN адаптер с интерфейсом, совместимым с ELM327."""

    def __init__(self, port, bitrate=500, timeout=2.0, verbose=False):
        self.port = port
        self.bitrate = bitrate
        self.timeout = timeout
        self.verbose = verbose
        self.link = None
        self.current_header = 0x7E0
        self.rx_filter = None
        self.raw_log = []
        self.unsupported = []
        self.buffer = ""

    # -- соединение --------------------------------------------------------
    def connect(self):
        self.link = SerialChannel(self.port, baudrate=115200, timeout=0.4)
        self._raw("C")                       # закрыть, если было открыто
        time.sleep(0.1)
        self._raw(BITRATES.get(self.bitrate, "S6"))
        self._raw("O")                       # открыть шину
        time.sleep(0.1)
        self._drain()

    def close(self):
        if self.link:
            try:
                self._raw("C")
                self.link.close()
            except Exception:                # noqa: BLE001
                pass
            self.link = None

    def _raw(self, command):
        """Отправляет служебную команду адаптеру."""
        self.link.sendall((command + "\r").encode("ascii"))
        time.sleep(0.03)

    def _drain(self):
        end = time.time() + 0.15
        while time.time() < end:
            try:
                self.link.recv(512)
            except (TimeoutError, OSError):
                break

    # -- приём кадров ------------------------------------------------------
    def _read_frames(self, timeout):
        """
        Читает кадры, пришедшие за отведённое время.
        Формат строки: t<идентификатор><длина><данные>
        """
        frames = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.link.recv(512)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                continue
            self.buffer += chunk.decode("ascii", errors="replace")
            while "\r" in self.buffer:
                line, self.buffer = self.buffer.split("\r", 1)
                line = line.strip()
                if len(line) >= 5 and line[0] in "tT":
                    width = 3 if line[0] == "t" else 8
                    try:
                        can_id = int(line[1:1 + width], 16)
                        length = int(line[1 + width], 16)
                        payload = bytes.fromhex(line[2 + width:2 + width + length * 2])
                    except ValueError:
                        continue
                    frames.append((can_id, payload))
                    if frames:
                        deadline = min(deadline, time.time() + 0.25)
        return frames

    def _send_frame(self, can_id, payload):
        body = payload.ljust(8, b"\x00")
        line = f"t{can_id:03X}{len(body)}{body.hex().upper()}"
        self._raw(line)

    # -- обмен по ISO-TP ---------------------------------------------------
    def _transfer(self, payload):
        """
        Полный обмен: запрос, приём первого кадра, отправка разрешения,
        сбор продолжения. То, чего не умеет дешёвый ELM327.
        """
        tx_id = self.current_header
        self._send_frame(tx_id, bytes([len(payload)]) + payload)

        frames = self._read_frames(1.2)
        if not frames:
            return None, b""

        source, first = frames[0]
        pci = first[0] >> 4

        if pci == 0x0:                                    # одиночный кадр
            return source, first[1:1 + (first[0] & 0x0F)]

        if pci == 0x1:                                    # первый кадр
            total = ((first[0] & 0x0F) << 8) | first[1]
            data = bytearray(first[2:])
            # Кадр разрешения: передавать всё подряд, без задержек
            self._send_frame(tx_id, bytes([0x30, 0x00, 0x00]))
            deadline = time.time() + 2.5
            while len(data) < total and time.time() < deadline:
                for can_id, payload_in in self._read_frames(0.6):
                    if can_id != source:
                        continue
                    if payload_in[0] >> 4 == 0x2:
                        data += payload_in[1:]
            return source, bytes(data[:total])

        return source, b""

    # -- интерфейс, совместимый с ELM327 -----------------------------------
    def init(self):
        pass                                  # настройка выполнена при открытии

    def identify(self):
        return f"USB-CAN {self.bitrate} кбит/с", "—"

    def accept_all(self):
        self.rx_filter = None

    def set_rx_filter(self, can_id):
        self.rx_filter = can_id

    def auto_receive(self):
        self.rx_filter = None

    def apply_mf_strategy(self, name, tx_id, rx_id):
        pass                                  # сборка кадров своя, приёмы не нужны

    def setup_multiframe(self, tx_id, rx_id):
        pass

    def set_header(self, can_id):
        self.current_header = can_id

    def cmd(self, text, read_timeout=1.2):
        """
        Принимает запрос в том же виде, что и ELM327, и возвращает ответ
        строками того же формата. Служебные команды ELM просто
        подтверждаются: настраивать здесь нечего.
        """
        command = text.strip().upper()
        if command.startswith("AT"):
            answer = ["OK"]
            if command == "ATI":
                answer = [f"USB-CAN SLCAN {self.bitrate}k"]
            elif command == "ATRV":
                answer = ["—"]
            elif command.startswith("ATSH"):
                try:
                    self.current_header = int(command[4:], 16)
                except ValueError:
                    pass
            self.raw_log.append((text, " ".join(answer)))
            return answer

        try:
            payload = bytes.fromhex(command)
        except ValueError:
            return ["?"]

        source, data = self._transfer(payload)
        if not data:
            self.raw_log.append((text, "NO DATA"))
            return ["NO DATA"]

        lines = self._format_as_frames(source, data)
        self.raw_log.append((text, " | ".join(lines)))
        if self.verbose:
            print(f"      >> {text}  << {lines}")
        return lines

    @staticmethod
    def _format_as_frames(source, data):
        """
        Раскладывает собранный ответ обратно в кадры того же вида,
        что печатает ELM327. Так весь разбор в программе остаётся общим.
        """
        if len(data) <= 7:
            return [f"{source:03X}{len(data):02X}{data.hex().upper()}"]
        out = [f"{source:03X}1{len(data):03X}{data[:6].hex().upper()}"]
        rest, counter = data[6:], 1
        while rest:
            chunk, rest = rest[:7], rest[7:]
            out.append(f"{source:03X}2{counter:X}{chunk.hex().upper()}")
            counter = (counter + 1) & 0x0F
        return out
