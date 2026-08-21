package ru.vagdiag

import android.annotation.SuppressLint
import android.app.Application
import android.app.PendingIntent
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import ru.vagdiag.core.*
import kotlin.coroutines.resume
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Найденный блок управления. */
data class ModuleInfo(
    val canId: Int,
    val hex: String = "0x%03X".format(canId),
    val rx: String = "",
    val name: String = "",
    val part: String = "",
    val faults: Int? = null,
    val firstCode: String = "",
)

/** Строка разбора ошибок по признаку. */
data class FaultRow(val title: String, val count: Int?, val code: String, val failure: String)

/** Замер монитора: столько-то активных ошибок в такой-то момент. */
data class MonitorSample(
    val time: String, val active: Int?, val total: Int?,
    val code: String, val event: String,
)

/** Коды двигателя с описанием. */
data class EngineCode(val code: String, val text: String, val serious: Boolean)

data class EngineInfo(
    val vin: String = "",
    val stored: List<EngineCode> = emptyList(),
    val pending: List<EngineCode> = emptyList(),
    val permanent: List<EngineCode> = emptyList(),
    val readiness: Obd.Readiness? = null,
    val read: Boolean = false,
)

data class LiveValue(val title: String, val value: Double, val unit: String)

data class AppState(
    val connected: Boolean = false,
    val busy: Boolean = false,
    val task: String = "",
    val progress: Int = 0,
    val message: String = "Не подключено",
    val adapterTitle: String = "",
    val adapterVersion: String = "",
    val voltage: String = "",
    val warning: String = "",
    val modules: List<ModuleInfo> = emptyList(),
    val details: Map<String, List<FaultRow>> = emptyMap(),
    val engine: EngineInfo = EngineInfo(),
    val live: List<LiveValue> = emptyList(),
    val liveRunning: Boolean = false,
    val monitor: List<MonitorSample> = emptyList(),
    val monitorRunning: Boolean = false,
    val wizard: List<MonitorSample> = emptyList(),
    val log: List<String> = emptyList(),
    val bondedDevices: List<Pair<String, String>> = emptyList(),
    /** Найденные USB-адаптеры: системное имя устройства и понятное название. */
    val usbDevices: List<Pair<String, String>> = emptyList(),
    /** Вкладывать ли в отчёт весь обмен с адаптером. По умолчанию нет:
     *  обмен занимает сотни строк и мессенджер такое письмо режет. */
    val reportTrace: Boolean = false,
)

/**
 * Вся логика работы. Задачи выполняются строго по одной: адаптер один,
 * параллелить нечего, а два запроса подряд он не переносит.
 */
class DiagViewModel(application: Application) : AndroidViewModel(application) {

    private val _state = MutableStateFlow(AppState())
    val state: StateFlow<AppState> = _state.asStateFlow()

    private var elm: Elm327? = null
    private var worker: Job? = null
    private var loopJob: Job? = null

    private val clock = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    // Известные адреса блоков VAG для быстрого поиска
    private val knownIds = listOf(
        0x700, 0x703, 0x70A, 0x711, 0x712, 0x713, 0x714, 0x715, 0x716,
        0x717, 0x71E, 0x744, 0x746, 0x74F, 0x754, 0x767, 0x76E, 0x773,
        0x776, 0x77E, 0x7E0, 0x7E1, 0x7F1,
    )

    private fun say(text: String) = _state.update {
        it.copy(message = text, log = (it.log + "${clock.format(Date())}  $text").takeLast(200))
    }

    private fun progress(value: Int) = _state.update { it.copy(progress = value) }

    /** Ставит задачу в очередь. Пока идёт одна, вторая не запускается. */
    private fun run(title: String, block: suspend () -> Unit) {
        if (_state.value.busy) {
            say("Дождитесь окончания: $title")
            return
        }
        _state.update { it.copy(busy = true, task = title, progress = 0) }
        worker = viewModelScope.launch(Dispatchers.IO) {
            try {
                block()
            } catch (error: Exception) {
                say("Ошибка: ${error.message}")
            } finally {
                _state.update { it.copy(busy = false, task = "", progress = 100) }
            }
        }
    }

    // -- подключение --------------------------------------------------------

    @SuppressLint("MissingPermission")
    fun loadBondedDevices() {
        val manager = getApplication<Application>()
            .getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        val devices = runCatching {
            manager?.adapter?.bondedDevices?.map { it.address to (it.name ?: it.address) }
        }.getOrNull().orEmpty()
        _state.update { it.copy(bondedDevices = devices) }
        if (devices.isEmpty()) {
            say("Сопряжённых устройств Bluetooth не найдено")
        }
    }

    fun connectWifi(host: String, port: Int) = run("Подключение") {
        openTransport(WifiTransport(host, port))
    }

    // -- USB через кабель OTG ----------------------------------------------

    private val usbAction = "ru.vagdiag.USB_PERMISSION"

    /** Ищет подключённые адаптеры, микросхему которых драйвер узнаёт. */
    fun loadUsbDevices() {
        val manager = getApplication<Application>()
            .getSystemService(Context.USB_SERVICE) as? UsbManager
        val prober = com.hoho.android.usbserial.driver.UsbSerialProber.getDefaultProber()
        val devices = manager?.deviceList?.values.orEmpty()
            .filter { prober.probeDevice(it) != null }
            .map { device ->
                device.deviceName to (
                    runCatching { device.productName }.getOrNull()
                        ?: "USB %04X:%04X".format(device.vendorId, device.productId))
            }
        _state.update { it.copy(usbDevices = devices) }
        if (devices.isEmpty()) {
            say("USB-адаптеров не найдено. Нужен кабель OTG, а сам адаптер " +
                "должен быть запитан от разъёма OBD.")
        }
    }

    fun connectUsb(deviceName: String) = run("Подключение") {
        val manager = getApplication<Application>()
            .getSystemService(Context.USB_SERVICE) as? UsbManager
        if (manager == null) {
            say("Телефон не умеет работать в режиме USB-хоста")
            return@run
        }
        val device = manager.deviceList[deviceName]
        if (device == null) {
            say("Устройство отключено, обновите список")
            return@run
        }
        // Разрешение спрашивается один раз на устройство. Если приложение
        // открылось само при подключении адаптера, оно уже выдано.
        if (!manager.hasPermission(device)) {
            say("Запрашиваю доступ к адаптеру")
            if (!askUsbPermission(manager, device)) {
                say("Доступ к USB-устройству не выдан")
                return@run
            }
        }
        openTransport(UsbSerialTransport(manager, device))
    }

    private suspend fun askUsbPermission(manager: UsbManager, device: UsbDevice): Boolean =
        suspendCancellableCoroutine { waiting ->
            val app = getApplication<Application>()
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(context: Context?, intent: Intent?) {
                    runCatching { app.unregisterReceiver(this) }
                    val granted = intent?.getBooleanExtra(
                        UsbManager.EXTRA_PERMISSION_GRANTED, false) ?: false
                    if (waiting.isActive) waiting.resume(granted)
                }
            }
            val filter = IntentFilter(usbAction)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                app.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                app.registerReceiver(receiver, filter)
            }
            // FLAG_MUTABLE обязателен: система дописывает в этот запрос
            // сведения об устройстве, иначе ответ приходит пустым.
            var flags = PendingIntent.FLAG_UPDATE_CURRENT
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                flags = flags or PendingIntent.FLAG_MUTABLE
            }
            val pending = PendingIntent.getBroadcast(
                app, 0, Intent(usbAction).setPackage(app.packageName), flags)
            waiting.invokeOnCancellation { runCatching { app.unregisterReceiver(receiver) } }
            manager.requestPermission(device, pending)
        }

    @SuppressLint("MissingPermission")
    fun connectBluetooth(address: String) = run("Подключение") {
        val manager = getApplication<Application>()
            .getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
        val device: BluetoothDevice? = runCatching {
            manager?.adapter?.getRemoteDevice(address)
        }.getOrNull()
        if (device == null) {
            say("Устройство не найдено")
            return@run
        }
        openTransport(BluetoothTransport(device))
    }

    private fun openTransport(transport: Transport) {
        disconnectInternal()
        say("Подключение: ${transport.title}")
        val adapter = Elm327(transport)
        try {
            adapter.connect()
        } catch (error: Exception) {
            say("Не удалось подключиться: ${error.message}")
            return
        }
        adapter.init()
        val (version, voltage) = adapter.identify()

        val warning = runCatching {
            val value = voltage.uppercase().replace("V", "").trim().toDouble()
            when {
                value < 11.5 -> "Напряжение ниже 11,5 В — возможны ложные ошибки"
                value > 13.3 -> "Двигатель запущен, для диагностики лучше заглушить"
                else -> ""
            }
        }.getOrDefault("")

        elm = adapter
        _state.update {
            it.copy(connected = true, adapterTitle = transport.title,
                    adapterVersion = version, voltage = voltage, warning = warning)
        }
        say("Адаптер $version, бортсеть $voltage")
    }

    fun disconnect() = run("Отключение") { disconnectInternal(); say("Отключено") }

    private fun disconnectInternal() {
        loopJob?.cancel()
        runCatching { elm?.close() }
        elm = null
        _state.update {
            it.copy(connected = false, adapterTitle = "", adapterVersion = "",
                    voltage = "", warning = "", monitorRunning = false,
                    liveRunning = false)
        }
    }

    // -- поиск блоков -------------------------------------------------------

    fun scan(full: Boolean) = run("Поиск блоков") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        val ids = if (full) (0x700..0x7FF).toList() else knownIds
        say("Проверяется адресов: ${ids.size}")
        adapter.acceptAll()

        val found = mutableListOf<ModuleInfo>()
        ids.forEachIndexed { index, canId ->
            adapter.setHeader(canId)
            val lines = adapter.command("3E00", 700)
            if (!IsoTp.isNegative(lines)) {
                val (source, data) = IsoTp.assemble(lines)
                if (data.isNotEmpty()) {
                    found += ModuleInfo(canId, rx = source?.let { "0x%03X".format(it) } ?: "")
                    _state.update { it.copy(modules = found.toList()) }
                    say("Найден блок 0x%03X".format(canId))
                }
            }
            progress((index + 1) * 100 / ids.size)
        }
        say("Найдено блоков: ${found.size}")
    }

    /** Собирает имя, номер запчасти и число ошибок по каждому блоку. */
    fun collectAll() = run("Сбор данных") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        val modules = _state.value.modules.toMutableList()
        if (modules.isEmpty()) return@run say("Сначала выполните поиск блоков")

        modules.forEachIndexed { index, item ->
            adapter.acceptAll()
            val uds = UdsClient(adapter, item.canId)
            if (!uds.testerPresent()) {
                modules[index] = item.copy(name = "нет ответа")
                _state.update { it.copy(modules = modules.toList()) }
                return@forEachIndexed
            }
            uds.startSession()
            val name = runCatching { uds.readText(0xF197) }.getOrDefault("")
            val part = runCatching { uds.readText(0xF187) }.getOrDefault("")
            val brief = uds.faultBrief(0xFF)

            // Видеть, каким способом удалось дочитать длинный ответ, полезно:
            // по этой строке сразу понятно, насколько адаптеру можно верить.
            uds.mfStrategy?.let { say("${item.hex}: длинный ответ дочитан — ${mfName(it)}") }

            modules[index] = item.copy(
                rx = uds.rxId?.let { "0x%03X".format(it) } ?: item.rx,
                name = name, part = part,
                faults = brief?.count,
                firstCode = listOf(brief?.code, brief?.failure)
                    .filterNot { it.isNullOrBlank() }.joinToString(" "),
            )
            _state.update { it.copy(modules = modules.toList()) }
            progress((index + 1) * 100 / modules.size)
            Thread.sleep(200)
        }
        say("Данные собраны")
    }

    /** Разбирает ошибки блока по каждому признаку. */
    fun readFaults(canId: Int) = run("Разбор ошибок") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        adapter.acceptAll()
        val uds = UdsClient(adapter, canId)
        if (!uds.testerPresent()) return@run say("Блок не отвечает")
        uds.startSession()

        val rows = mutableListOf<FaultRow>()
        Dtc.masks.forEachIndexed { index, (mask, title) ->
            val brief = uds.faultBrief(mask)
            rows += FaultRow(title, brief?.count, brief?.code ?: "", brief?.failure ?: "")
            progress((index + 1) * 100 / Dtc.masks.size)
            Thread.sleep(1500)   // блок остаётся занят после обрыва длинной передачи
        }
        _state.update {
            it.copy(details = it.details + ("0x%03X".format(canId) to rows))
        }
        say("Разбор завершён")
    }

    /** Человеческое название способа дочитывания длинного ответа. */
    private fun mfName(key: String): String = when (key) {
        "cra" -> "адаптер справился сам (ATCRA)"
        "cra_fc" -> "адаптеру задан кадр разрешения вручную"
        "manual" -> "сырые кадры, разрешение шлёт приложение"
        else -> key
    }

    fun clearFaults(canId: Int) = run("Стирание ошибок") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        adapter.acceptAll()
        val uds = UdsClient(adapter, canId)
        if (!uds.testerPresent()) return@run say("Блок не отвечает")
        val ok = uds.clearFaults()
        say(if (ok) "Память ошибок стёрта" else "Блок отказал в стирании")
    }

    // -- двигатель ----------------------------------------------------------

    fun readEngine() = run("Диагностика двигателя") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        adapter.acceptAll()

        say("Чтение VIN"); progress(15)
        val vin = Obd.readVin(adapter)

        say("Чтение кодов"); progress(40)
        fun decorate(codes: List<String>) =
            codes.map { EngineCode(it, Codes.describe(it), Codes.isSerious(it)) }

        val stored = decorate(Obd.readDtcs(adapter, 0x03)); progress(60)
        val pending = decorate(Obd.readDtcs(adapter, 0x07)); progress(75)
        val permanent = decorate(Obd.readDtcs(adapter, 0x0A)); progress(85)

        say("Проверка готовности систем")
        val readiness = Obd.readReadiness(adapter)

        _state.update {
            it.copy(engine = EngineInfo(vin, stored, pending, permanent, readiness, true))
        }
        say("Готово. Кодов: ${stored.size + pending.size + permanent.size}")
    }

    fun clearEngine() = run("Стирание кодов двигателя") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        adapter.acceptAll()
        val ok = Obd.clearDtcs(adapter)
        say(if (ok) "Коды двигателя стёрты" else "Блок отказал в стирании")
    }

    // -- живые параметры ----------------------------------------------------

    fun startLive() {
        val adapter = elm ?: return say("Сначала подключитесь")
        if (_state.value.liveRunning) return
        _state.update { it.copy(liveRunning = true) }
        loopJob = viewModelScope.launch(Dispatchers.IO) {
            adapter.acceptAll()
            say("Определяю поддерживаемые параметры")
            val supported = Obd.supportedPids(adapter)
            val list = Obd.pids.keys.filter { it in supported }
                .ifEmpty { listOf(0x05, 0x0C, 0x0D, 0x11, 0x42) }
            say("Доступно параметров: ${list.size}")

            // Блок объявляет список поддерживаемых параметров сам, но врёт:
            // часть объявленных отвечает NO DATA. Спрашивать их в каждом
            // круге — впустую тратить время цикла, а круг и так не быстрый.
            // Три отказа подряд — и параметр выбывает до следующего запуска.
            val alive = list.toMutableList()
            val misses = mutableMapOf<Int, Int>()

            while (isActive && _state.value.liveRunning) {
                val values = mutableListOf<LiveValue>()
                for (pid in alive.toList()) {
                    val answer = Obd.readPid(adapter, pid)
                    if (answer == null) {
                        val n = (misses[pid] ?: 0) + 1
                        misses[pid] = n
                        if (n >= 3) {
                            alive.remove(pid)
                            say("Параметр 0x%02X молчит, исключён из опроса".format(pid))
                        }
                    } else {
                        misses[pid] = 0
                        val (title, value, unit) = answer
                        values += LiveValue(title, value, unit)
                    }
                }
                _state.update { it.copy(live = values) }
                if (alive.isEmpty()) {
                    say("Ни один параметр не отвечает, опрос остановлен")
                    _state.update { it.copy(liveRunning = false) }
                    break
                }
                Thread.sleep(400)
            }
        }
    }

    fun stopLive() {
        _state.update { it.copy(liveRunning = false) }
        loopJob?.cancel()
    }

    // -- монитор ------------------------------------------------------------

    /**
     * Живой счётчик активных ошибок.
     *
     * Придуман для задачи «какой из датчиков неисправен» и решает её
     * без снятия бампера: отключаете все датчики, потом подключаете
     * по одному и смотрите, на каком счётчик перестаёт уменьшаться.
     */
    fun startMonitor(canId: Int) {
        val adapter = elm ?: return say("Сначала подключитесь")
        if (_state.value.monitorRunning) return
        _state.update { it.copy(monitorRunning = true, monitor = emptyList()) }

        loopJob = viewModelScope.launch(Dispatchers.IO) {
            adapter.acceptAll()
            val uds = UdsClient(adapter, canId)
            if (!uds.testerPresent()) {
                say("Блок не отвечает")
                _state.update { it.copy(monitorRunning = false) }
                return@launch
            }
            say("Монитор запущен")
            var previous: Int? = null

            while (isActive && _state.value.monitorRunning) {
                val active = uds.faultBrief(0x01)
                Thread.sleep(1200)
                val total = uds.faultBrief(0xFF)

                val event = if (previous != null && active != null && active.count != previous)
                    "изменилось на %+d".format(active.count - previous!!) else ""
                if (active != null) previous = active.count

                val sample = MonitorSample(
                    clock.format(Date()), active?.count, total?.count,
                    listOf(active?.code, active?.failure)
                        .filterNot { it.isNullOrBlank() }.joinToString(" "),
                    event)
                _state.update { it.copy(monitor = (it.monitor + sample).takeLast(60)) }
                Thread.sleep(1300)
            }
            say("Монитор остановлен")
        }
    }

    fun stopMonitor() {
        _state.update { it.copy(monitorRunning = false) }
        loopJob?.cancel()
    }

    /** Шаг мастера: подписывает текущее показание счётчика. */
    fun wizardSample(canId: Int, label: String) = run("Замер") {
        val adapter = elm ?: return@run say("Сначала подключитесь")
        adapter.acceptAll()
        val uds = UdsClient(adapter, canId)
        if (!uds.testerPresent()) return@run say("Блок не отвечает")

        val active = uds.faultBrief(0x01)
        Thread.sleep(1000)
        val total = uds.faultBrief(0xFF)

        val previous = _state.value.wizard.lastOrNull()?.active
        val delta = if (previous != null && active != null)
            "%+d".format(active.count - previous) else ""

        val sample = MonitorSample(
            clock.format(Date()), active?.count, total?.count,
            listOf(active?.code, active?.failure)
                .filterNot { it.isNullOrBlank() }.joinToString(" "),
            "$label $delta".trim())
        _state.update { it.copy(wizard = it.wizard + sample) }
        say("Шаг записан: $label")
    }

    fun wizardReset() = _state.update { it.copy(wizard = emptyList()) }

    /** Собирает всё в один текст — его можно переслать для разбора. */
    fun buildReport(): String = buildString {
        val snapshot = _state.value
        appendLine("=".repeat(60))
        appendLine("ОТЧЁТ ДЛЯ ОТПРАВКИ")
        appendLine("приложение   : Android, версия 1.0")
        val stamp = SimpleDateFormat("dd.MM.yyyy HH:mm", Locale.getDefault())
        appendLine("время        : ${stamp.format(Date())}")
        appendLine("адаптер      : ${snapshot.adapterVersion}, ${snapshot.adapterTitle}")
        appendLine("бортсеть     : ${snapshot.voltage}")
        appendLine("=".repeat(60))

        if (snapshot.engine.read) {
            appendLine()
            appendLine("ДВИГАТЕЛЬ, СТАНДАРТ OBD-II")
            appendLine("-".repeat(60))
            appendLine("VIN: ${snapshot.engine.vin.ifBlank { "—" }}")
            fun codes(title: String, list: List<EngineCode>) =
                appendLine("$title: " + (list.joinToString(", ") { "${it.code} — ${it.text}" }
                    .ifBlank { "нет" }))
            codes("Сохранённые", snapshot.engine.stored)
            codes("Неподтверждённые", snapshot.engine.pending)
            codes("Постоянные", snapshot.engine.permanent)
            snapshot.engine.readiness?.let { readiness ->
                appendLine("Лампа неисправности: " + if (readiness.mil) "горит" else "погашена")
                readiness.checks.forEach { appendLine("  ${it.first}: ${it.second}") }
            }
        }

        appendLine()
        appendLine("БЛОКИ")
        appendLine("-".repeat(60))
        snapshot.modules.forEach {
            appendLine("${it.hex}  ответ с ${it.rx.ifBlank { "—" }}  " +
                    "имя: ${it.name.ifBlank { "—" }}  " +
                    "номер: ${it.part.ifBlank { "—" }}  " +
                    "ошибок: ${it.faults ?: "—"}  ${it.firstCode}")
        }

        snapshot.details.forEach { (hex, rows) ->
            appendLine()
            appendLine("РАЗБОР ОШИБОК $hex")
            appendLine("-".repeat(60))
            rows.forEach {
                val count = (it.count?.toString() ?: "—").padStart(4)
                appendLine("${it.title.padEnd(20)} $count  ${it.code} ${it.failure}")
            }
        }

        if (snapshot.wizard.isNotEmpty()) {
            appendLine()
            appendLine("МАСТЕР ПОИСКА ДАТЧИКА")
            appendLine("-".repeat(60))
            snapshot.wizard.forEach {
                appendLine("${it.time}  активных ${it.active}  ${it.code}  ${it.event}")
            }
        }

        appendLine()
        if (snapshot.reportTrace) {
            appendLine("ПОЛНЫЙ ОБМЕН С АДАПТЕРОМ")
            appendLine("-".repeat(60))
            elm?.rawLog?.forEach { (request, answer) ->
                appendLine(">> $request")
                appendLine("<< $answer")
            }
        } else {
            val lines = elm?.rawLog?.size ?: 0
            appendLine("Полный обмен с адаптером не вложен ($lines команд).")
            appendLine("Включите переключатель на вкладке «Связь», если отчёт")
            appendLine("нужен для разбора неисправности.")
        }
    }

    /** Переключатель «вкладывать полный обмен в отчёт». */
    fun setReportTrace(on: Boolean) {
        _state.update { it.copy(reportTrace = on) }
    }

    override fun onCleared() {
        super.onCleared()
        disconnectInternal()
    }
}
