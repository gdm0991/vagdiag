package ru.vagdiag.core

/** Отрицательный ответ блока или отсутствие ответа. */
class UdsError(message: String) : Exception(message)

/** Кадр разрешения: «шли всё сразу, без пауз между кадрами». */
private const val FLOW_CONTROL = "3000000000000000"

/**
 * Клиент протокола UDS — на нём разговаривают блоки управления VAG.
 */
class UdsClient(private val elm: Elm327, val txId: Int) {

    var rxId: Int? = null
        private set

    /**
     * True, если последний ответ пришёл обрезанным: заголовок обещал больше
     * байтов, чем адаптер довёз. Так ведёт себя ELM327 с блоками кузова —
     * он не запрашивает продолжение ни у кого, кроме двигателя.
     * Нужен, чтобы не выдавать огрызок за настоящие данные.
     */
    var lastTruncated: Boolean = false
        private set

    /** Какой способ дочитывания длинного ответа сработал на этом блоке. */
    var mfStrategy: String? = null
        private set

    private val mfTried = mutableSetOf<String>()

    private val nrcText = mapOf(
        0x10 to "общий отказ",
        0x11 to "сервис не поддерживается",
        0x12 to "подфункция не поддерживается",
        0x13 to "неверная длина запроса",
        0x21 to "блок занят, нужен повтор запроса",
        0x22 to "условия не выполнены",
        0x31 to "запрос вне диапазона",
        0x33 to "требуется доступ по паролю",
        0x78 to "ответ будет позже",
        0x7F to "сервис недоступен в текущей сессии",
    )

    /**
     * Отправляет запрос и возвращает данные положительного ответа.
     *
     * Повторяет при ответах «занят» и «ответ будет позже»: блоки кузовной
     * электроники часто отвечают так на первое обращение после пробуждения,
     * и одна попытка дала бы ложный вывод, что блок мёртв.
     */
    fun request(payloadHex: String, timeoutMs: Long = 1500, attempts: Int = 3): ByteArray {
        var lastError = "нет ответа от блока"
        repeat(attempts) { attempt ->
            elm.setHeader(txId)
            val lines = elm.command(payloadHex, timeoutMs)
            lastTruncated = IsoTp.isTruncated(lines)

            // Пришёл только первый кадр. Раньше на этом всё и заканчивалось —
            // приложение довольствовалось огрызком. Теперь перебираются те же
            // способы, что и в версии для компьютера: вдруг адаптер окажется
            // не таким безнадёжным, как тот, на котором всё это выяснялось.
            if (lastTruncated && rxId != null && !IsoTp.isNegative(lines)) {
                val full = retryLong(payloadHex, timeoutMs)
                if (full != null) {
                    lastTruncated = false
                    return full
                }
                lastError = "длинный ответ дочитать не удалось"
                return@repeat
            }

            if (IsoTp.isNegative(lines)) {
                lastError = "нет ответа от блока"
                Thread.sleep(300)
                return@repeat
            }

            val (source, data) = IsoTp.assemble(lines)
            if (source != null) rxId = source
            if (data.isEmpty()) {
                lastError = "ответ не разобран"
                Thread.sleep(200)
                return@repeat
            }

            if (data[0].toInt() and 0xFF != 0x7F) return data

            val nrc = if (data.size > 2) data[2].toInt() and 0xFF else 0
            if (nrc == 0x21 || nrc == 0x78) {
                Thread.sleep(400 + 200L * attempt)
                lastError = "отказ блока: ${nrcText[nrc]}"
                return@repeat
            }
            throw UdsError("отказ блока: ${nrcText[nrc] ?: "код 0x%02X".format(nrc)}")
        }
        throw UdsError(lastError)
    }

    /**
     * Перебирает способы получить длинный ответ целиком.
     *
     * Порядок важен. Сначала то, что делает сам адаптер, потом то, что
     * приходится делать за него. Сработавший способ запоминается: второй раз
     * перебирать не нужно, а лишние команды на дешёвых адаптерах опасны.
     */
    private fun retryLong(payloadHex: String, timeoutMs: Long): ByteArray? {
        val target = rxId ?: return null
        val order = mfStrategy?.let { listOf(it) }
            ?: listOf("cra", "cra_fc").filter { it !in mfTried }
        try {
            for (name in order) {
                mfTried += name
                elm.applyMfStrategy(name, txId, target)
                Thread.sleep(250)
                val lines = elm.command(payloadHex, timeoutMs + 1000)
                if (IsoTp.isNegative(lines) || IsoTp.isTruncated(lines)) continue
                val (_, data) = IsoTp.assemble(lines)
                if (data.isNotEmpty() && data[0].toInt() and 0xFF != 0x7F) {
                    mfStrategy = name
                    return data
                }
            }
            if ("manual" !in mfTried) {
                mfTried += "manual"
                val data = runCatching { requestManual(payloadHex) }.getOrNull()
                if (data != null) {
                    mfStrategy = "manual"
                    return data
                }
            }
            return null
        } finally {
            // Фильтр обязательно снимается: иначе следующий блок,
            // отвечающий с другого адреса, окажется не слышен.
            elm.acceptAll()
        }
    }

    /**
     * Запрос сырыми кадрами: кадр разрешения на продолжение отправляет
     * не адаптер, а программа.
     *
     * Прошивка ELM327 ведёт многокадровый обмен только с парой
     * «запрос 0x7E0 — ответ 0x7E8». Блокам кузова, отвечающим с другим
     * смещением, разрешение никто не посылает: блок отдаёт первый кадр
     * и ждёт, а следующие запросы отклоняет с ответом «занят», потому что
     * прошлая передача так и не завершилась.
     */
    private fun requestManual(payloadHex: String, timeoutMs: Long = 2500,
                              attempts: Int = 3): ByteArray {
        elm.acceptAll()
        elm.rawFrames(true)
        try {
            elm.forgetHeader()
            elm.setHeader(txId)
            val frame = singleFrame(payloadHex)
            repeat(attempts) {
                val lines = elm.command(frame, timeoutMs)
                if (IsoTp.isNegative(lines)) {
                    Thread.sleep(600)   // блок ещё занят прошлой передачей
                    return@repeat
                }
                var collected = lines
                if (IsoTp.isTruncated(lines)) {
                    collected = collected + elm.command(FLOW_CONTROL, timeoutMs)
                }
                val (source, data) = IsoTp.assemble(collected)
                if (data.isEmpty()) {
                    Thread.sleep(400)
                    return@repeat
                }
                if (data[0].toInt() and 0xFF == 0x7F) {
                    val nrc = if (data.size > 2) data[2].toInt() and 0xFF else 0
                    if (nrc == 0x21 || nrc == 0x78) {
                        Thread.sleep(600)
                        return@repeat
                    }
                    throw UdsError("отказ блока: ${nrcText[nrc] ?: "код 0x%02X".format(nrc)}")
                }
                if (source != null) rxId = source
                return data
            }
            throw UdsError("нет ответа в режиме сырых кадров")
        } finally {
            elm.rawFrames(false)
            elm.forgetHeader()
        }
    }

    /**
     * Одиночный кадр ISO-TP: длина, полезная нагрузка, добивка нулями до
     * восьми байт. Нулями, а не 0xAA: заполнитель блок всё равно
     * отбрасывает по указанной длине, а нули читаются в журнале яснее.
     */
    private fun singleFrame(payloadHex: String): String {
        val clean = payloadHex.replace(" ", "")
        val bytes = ByteArray(clean.length / 2) {
            clean.substring(it * 2, it * 2 + 2).toInt(16).toByte()
        }
        val frame = ByteArray(8)
        frame[0] = bytes.size.toByte()
        bytes.copyInto(frame, 1, 0, minOf(bytes.size, 7))
        return frame.joinToString("") { "%02X".format(it) }
    }

    /** Проверка связи. Заодно запоминает адрес, с которого блок отвечает. */
    fun testerPresent(): Boolean {
        repeat(3) {
            runCatching {
                elm.setHeader(txId)
                val lines = elm.command("3E00", 900)
                if (!IsoTp.isNegative(lines)) {
                    val (source, data) = IsoTp.assemble(lines)
                    if (data.isNotEmpty()) {
                        if (source != null) rxId = source
                        return true
                    }
                }
            }
            Thread.sleep(250)
        }
        return false
    }

    /** Открывает расширенную сессию: без неё блок не отдаёт часть данных. */
    fun startSession(): Boolean = runCatching { request("1003"); true }.getOrDefault(false)

    /** Читает текстовый идентификатор: имя блока, номер запчасти. */
    fun readText(did: Int): String {
        val data = request("22%04X".format(did))
        val truncated = lastTruncated
        if (data.size < 3 || data[0].toInt() and 0xFF != 0x62) throw UdsError("неожиданный ответ")
        val text = data.drop(3)
            .map { it.toInt() and 0xFF }
            .filter { it in 32..126 }
            .map { it.toChar() }
            .joinToString("")
            .trim()
        // Многоточие честно говорит: это не всё имя, адаптер оборвал ответ.
        // Без него «PAR» выглядит как полное имя блока, а это огрызок
        // от «PARKHILFE 4.0».
        return if (truncated && text.isNotEmpty()) "$text…" else text
    }

    /**
     * Число ошибок и первый код по указанному признаку.
     *
     * Количество берётся из заголовка ответа, поэтому определяется точно
     * даже когда список длинный и адаптер не дочитал его до конца.
     */
    fun faultBrief(mask: Int): FaultBrief? {
        elm.setHeader(txId)
        val lines = elm.command("1902%02X".format(mask), 2500)
        if (IsoTp.isNegative(lines)) return null
        val declared = IsoTp.declaredLength(lines) ?: return null
        val data = IsoTp.partial(lines)
        if (data.isEmpty() || data[0].toInt() and 0xFF != 0x59 || declared < 3) return null

        val total = (declared - 3) / 4
        val records = data.drop(3)
        if (records.size >= 3) {
            val code = byteArrayOf(records[0], records[1], records[2])
            return FaultBrief(total, Dtc.format(code), Dtc.failureText(code))
        }
        return FaultBrief(total, "", "")
    }

    /** Стирание памяти ошибок — единственная операция записи. */
    fun clearFaults(): Boolean {
        startSession()
        Thread.sleep(300)
        return runCatching { request("14FFFFFF", 3000); true }.getOrDefault(false)
    }

    data class FaultBrief(val count: Int, val code: String, val failure: String)
}

/**
 * Расшифровка кодов блоков VAG.
 *
 * Тип отказа важнее номера: он говорит, чем искать неисправность.
 * Обрыв прозванивают, замыкание проверяют на изоляцию, отсутствие связи
 * ищут замером питания.
 */
object Dtc {

    private val failureTypes = mapOf(
        0x00 to "тип не указан",
        0x11 to "замыкание цепи на массу",
        0x12 to "замыкание цепи на плюс",
        0x13 to "обрыв цепи",
        0x14 to "обрыв или замыкание на массу",
        0x16 to "напряжение ниже порога",
        0x17 to "напряжение выше порога",
        0x1A to "сопротивление ниже нормы",
        0x1B to "сопротивление выше нормы",
        0x21 to "сигнал ниже минимума",
        0x22 to "сигнал выше максимума",
        0x29 to "сигнал недостоверен",
        0x2F to "сигнал нестабилен",
        0x62 to "несовпадение сигналов",
        0x81 to "неверные данные",
        0x87 to "нет сообщений от узла",
        0x92 to "неправильная работа узла",
    )

    private const val LETTERS = "PCBU"

    fun format(code: ByteArray): String {
        if (code.size != 3) return "??"
        val first = code[0].toInt() and 0xFF
        val letter = LETTERS[(first shr 6) and 0x03]
        val digit = (first shr 4) and 0x03
        return "%c%d%X%02X-%02X".format(
            letter, digit, first and 0x0F, code[1].toInt() and 0xFF,
            code[2].toInt() and 0xFF)
    }

    fun failureText(code: ByteArray): String {
        if (code.size != 3) return "неизвестно"
        val type = code[2].toInt() and 0xFF
        return failureTypes[type] ?: "код 0x%02X".format(type)
    }

    fun statusText(status: Int): String {
        val marks = mutableListOf<String>()
        if (status and 0x01 != 0) marks += "активна сейчас"
        if (status and 0x02 != 0) marks += "сбой в этом цикле"
        if (status and 0x08 != 0) marks += "подтверждена"
        if (status and 0x10 != 0) marks += "не проверялась"
        if (status and 0x20 != 0) marks += "была ранее"
        return if (marks.isEmpty()) "нет признаков" else marks.joinToString(", ")
    }

    /** Признаки, по которым разбирается список ошибок. */
    val masks = listOf(
        0xFF to "все ошибки",
        0x01 to "активна сейчас",
        0x02 to "сбой в этом цикле",
        0x08 to "подтверждена",
        0x10 to "не проверялась",
        0x20 to "была ранее",
    )
}

/**
 * Стандарт OBD-II. Обязателен по законам о выбросах, в отличие от блоков
 * кузова, — но не «на любой машине с 2001 года», как проще было бы сказать.
 * В Европе EOBD требуется от бензиновых машин с 2001 года и от дизельных
 * с 2004, в США OBD-II — с 1996. У привезённых с других рынков разъём
 * может стоять, а режимы не отвечать.
 */
object Obd {

    const val ENGINE_TX = 0x7E0

    data class Pid(val title: String, val unit: String, val convert: (List<Int>) -> Double)

    val pids: Map<Int, Pid> = mapOf(
        0x04 to Pid("Нагрузка на двигатель", "%") { it.getOrElse(0) { 0 } * 100.0 / 255 },
        0x05 to Pid("Температура охлаждающей жидкости", "°C") { it.getOrElse(0) { 0 } - 40.0 },
        0x0C to Pid("Обороты двигателя", "об/мин") {
            ((it.getOrElse(0) { 0 } shl 8) or it.getOrElse(1) { 0 }) / 4.0
        },
        0x0D to Pid("Скорость автомобиля", "км/ч") { it.getOrElse(0) { 0 }.toDouble() },
        0x0F to Pid("Температура впускного воздуха", "°C") { it.getOrElse(0) { 0 } - 40.0 },
        0x10 to Pid("Массовый расход воздуха", "г/с") {
            ((it.getOrElse(0) { 0 } shl 8) or it.getOrElse(1) { 0 }) / 100.0
        },
        0x11 to Pid("Положение дроссельной заслонки", "%") { it.getOrElse(0) { 0 } * 100.0 / 255 },
        0x2F to Pid("Уровень топлива", "%") { it.getOrElse(0) { 0 } * 100.0 / 255 },
        0x42 to Pid("Напряжение бортсети", "В") {
            ((it.getOrElse(0) { 0 } shl 8) or it.getOrElse(1) { 0 }) / 1000.0
        },
        0x46 to Pid("Температура за бортом", "°C") { it.getOrElse(0) { 0 } - 40.0 },
        0x5C to Pid("Температура масла", "°C") { it.getOrElse(0) { 0 } - 40.0 },
    )

    private val readinessBits = listOf(
        0x01 to "Катализатор",
        0x02 to "Подогрев катализатора",
        0x04 to "Система улавливания паров топлива",
        0x08 to "Система вторичного воздуха",
        0x20 to "Кислородные датчики",
        0x40 to "Подогрев кислородных датчиков",
        0x80 to "Система рециркуляции газов",
    )

    private fun request(elm: Elm327, payload: String, timeoutMs: Long = 3000): ByteArray? {
        elm.setHeader(ENGINE_TX)
        val lines = elm.command(payload, timeoutMs)
        if (IsoTp.isNegative(lines)) return null
        val (_, data) = IsoTp.assemble(lines)
        return data.takeIf { it.isNotEmpty() }
    }

    fun decodeDtc(high: Int, low: Int): String {
        val letter = "PCBU"[(high shr 6) and 0x03]
        val digit = (high shr 4) and 0x03
        return "%c%d%X%02X".format(letter, digit, high and 0x0F, low)
    }

    /** Режимы: 0x03 сохранённые, 0x07 неподтверждённые, 0x0A постоянные. */
    fun readDtcs(elm: Elm327, mode: Int): List<String> {
        val data = request(elm, "%02X".format(mode)) ?: return emptyList()
        if (data[0].toInt() and 0xFF != mode + 0x40) return emptyList()
        var body = data.drop(1)
        if (body.size % 2 == 1) body = body.drop(1)
        val codes = mutableListOf<String>()
        var index = 0
        while (index + 1 < body.size) {
            val high = body[index].toInt() and 0xFF
            val low = body[index + 1].toInt() and 0xFF
            if (high != 0 || low != 0) codes += decodeDtc(high, low)
            index += 2
        }
        return codes
    }

    fun clearDtcs(elm: Elm327): Boolean {
        val data = request(elm, "04") ?: return false
        return data[0].toInt() and 0xFF == 0x44
    }

    fun readPid(elm: Elm327, pid: Int): Triple<String, Double, String>? {
        val descriptor = pids[pid] ?: return null
        val data = request(elm, "01%02X".format(pid), 2000) ?: return null
        if (data.size < 3 || data[0].toInt() and 0xFF != 0x41) return null
        val bytes = data.drop(2).map { it.toInt() and 0xFF }
        val value = runCatching { descriptor.convert(bytes) }.getOrNull() ?: return null
        return Triple(descriptor.title, Math.round(value * 100) / 100.0, descriptor.unit)
    }

    /** Спрашивает у двигателя, какие параметры он вообще отдаёт. */
    fun supportedPids(elm: Elm327): Set<Int> {
        val supported = mutableSetOf<Int>()
        listOf(0x00, 0x20, 0x40).forEach { base ->
            val data = request(elm, "01%02X".format(base), 2500) ?: return@forEach
            if (data.size < 6 || data[0].toInt() and 0xFF != 0x41) return@forEach
            var mask = 0L
            for (offset in 2..5) mask = (mask shl 8) or (data[offset].toLong() and 0xFF)
            for (bit in 0 until 32) {
                if (mask and (1L shl (31 - bit)) != 0L) supported += base + bit + 1
            }
        }
        return supported
    }

    data class Readiness(val mil: Boolean, val count: Int, val checks: List<Pair<String, String>>)

    fun readReadiness(elm: Elm327): Readiness? {
        val data = request(elm, "0101", 2500) ?: return null
        if (data.size < 6 || data[0].toInt() and 0xFF != 0x41) return null
        val payload = data.drop(2).map { it.toInt() and 0xFF }
        val mil = payload[0] and 0x80 != 0
        val count = payload[0] and 0x7F
        val checks = readinessBits.map { (bit, title) ->
            val present = payload[1] and bit != 0
            val incomplete = payload[2] and bit != 0
            title to when {
                !present -> "не поддерживается"
                incomplete -> "не завершено"
                else -> "готово"
            }
        }
        return Readiness(mil, count, checks)
    }

    fun readVin(elm: Elm327): String {
        val data = request(elm, "0902", 4000) ?: return ""
        if (data[0].toInt() and 0xFF != 0x49) return ""
        val bytes = data.drop(3).map { it.toInt() and 0xFF }
        // Хотя бы один непечатаемый байт означает, что кадры собрались
        // неверно. Раньше такие байты просто выбрасывались, и на экран
        // уходил обрубок вроде «XWcZJG00000» — он выглядит как VIN,
        // но им не является. Лучше честное «не прочитан».
        if (bytes.any { it !in 32..126 }) return ""
        val text = bytes.map { it.toChar() }.joinToString("").trim()
        val vin = if (text.length >= 17) text.takeLast(17) else text
        return if (isPlausibleVin(vin)) vin else ""
    }

    /**
     * Настоящий VIN — ровно 17 знаков, только цифры и латиница,
     * причём букв I, O и Q в нём не бывает: их исключили, чтобы
     * не путать с единицей и нулём.
     */
    private fun isPlausibleVin(vin: String): Boolean =
        vin.length == 17 && vin.all { it in '0'..'9' || it in "ABCDEFGHJKLMNPRSTUVWXYZ" }
}
