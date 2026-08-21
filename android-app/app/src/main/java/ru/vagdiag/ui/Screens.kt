package ru.vagdiag.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import ru.vagdiag.*

// Оформление вынесено сюда, чтобы менять цвета в одном месте
val BgColor = Color(0xFF0F1216)
val PanelColor = Color(0xFF171B21)
val Panel2Color = Color(0xFF1E242C)
val LineColor = Color(0xFF2A323C)
val TextColor = Color(0xFFE6EAEF)
val DimColor = Color(0xFF9AA5B1)
val AccentColor = Color(0xFF4ADE80)
val Accent2Color = Color(0xFF38BDF8)
val WarnColor = Color(0xFFFBBF24)
val BadColor = Color(0xFFF87171)

val DarkScheme = darkColorScheme(
    primary = Accent2Color,
    onPrimary = Color(0xFF08222F),
    background = BgColor,
    surface = PanelColor,
    onSurface = TextColor,
    surfaceVariant = Panel2Color,
    onSurfaceVariant = DimColor,
    error = BadColor,
)

@Composable
fun Card(title: String? = null, content: @Composable ColumnScope.() -> Unit) {
    Surface(
        color = PanelColor,
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp)
    ) {
        Column(Modifier.padding(16.dp)) {
            if (title != null) {
                Text(title, color = TextColor, fontWeight = FontWeight.SemiBold,
                     fontSize = 15.sp, modifier = Modifier.padding(bottom = 10.dp))
            }
            content()
        }
    }
}

@Composable
fun Hint(text: String) {
    Surface(color = Color(0xFF12202B), shape = RoundedCornerShape(8.dp),
            modifier = Modifier.fillMaxWidth().padding(top = 10.dp)) {
        Text(text, color = TextColor, fontSize = 13.sp, lineHeight = 19.sp,
             modifier = Modifier.padding(12.dp))
    }
}

@Composable
fun Stat(label: String, value: String, color: Color = TextColor) {
    Surface(color = Panel2Color, shape = RoundedCornerShape(10.dp),
            modifier = Modifier.padding(end = 8.dp)) {
        Column(Modifier.padding(14.dp)) {
            Text(label, color = DimColor, fontSize = 12.sp)
            Text(value, color = color, fontSize = 30.sp, fontWeight = FontWeight.Bold)
        }
    }
}

@Composable
fun Row2(left: String, right: String, rightColor: Color = TextColor) {
    Row(Modifier.fillMaxWidth().padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.SpaceBetween) {
        Text(left, color = DimColor, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Text(right, color = rightColor, fontSize = 13.sp,
             textAlign = TextAlign.End, fontFamily = FontFamily.Monospace)
    }
}

// ---------------------------------------------------------------------------

@Composable
fun ConnectScreen(state: AppState, model: DiagViewModel) {
    var host by remember { mutableStateOf("192.168.0.10") }
    var port by remember { mutableStateOf("35000") }
    // "wifi" | "bt" | "usb" — способ связи с адаптером
    var mode by remember { mutableStateOf("wifi") }
    var chosen by remember { mutableStateOf("") }

    Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {

        Card("Адаптер") {
            Row(verticalAlignment = Alignment.CenterVertically) {
                FilterChip(selected = mode == "wifi",
                           onClick = { mode = "wifi"; chosen = "" },
                           label = { Text("Wi-Fi") })
                Spacer(Modifier.width(8.dp))
                FilterChip(selected = mode == "bt",
                           onClick = { mode = "bt"; chosen = ""; model.loadBondedDevices() },
                           label = { Text("Bluetooth") })
                Spacer(Modifier.width(8.dp))
                FilterChip(selected = mode == "usb",
                           onClick = { mode = "usb"; chosen = ""; model.loadUsbDevices() },
                           label = { Text("USB") })
            }

            Spacer(Modifier.height(12.dp))

            if (mode == "wifi") {
                OutlinedTextField(host, { host = it }, label = { Text("Адрес") },
                                  singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(port, { port = it }, label = { Text("Порт") },
                                  singleLine = true, modifier = Modifier.fillMaxWidth())
                Spacer(Modifier.height(12.dp))
                Button(
                    onClick = {
                        model.connectWifi(host.trim(),
                                          port.trim().toIntOrNull() ?: 35000)
                    },
                    enabled = !state.busy, modifier = Modifier.fillMaxWidth()) {
                    Text("Подключиться")
                }
                Hint("Телефон должен быть подключён к сети Wi-Fi адаптера. " +
                     "Зажигание включено, двигатель заглушен.")
            } else if (mode == "usb") {
                if (state.usbDevices.isEmpty()) {
                    Text("USB-адаптеров не видно. Нужен кабель OTG, а сам адаптер " +
                         "питается от разъёма OBD — без зажигания он молчит.",
                         color = DimColor, fontSize = 13.sp)
                    Spacer(Modifier.height(10.dp))
                    OutlinedButton(onClick = { model.loadUsbDevices() },
                                   modifier = Modifier.fillMaxWidth()) {
                        Text("Обновить список")
                    }
                } else {
                    state.usbDevices.forEach { (path, name) ->
                        val tint = if (chosen == path) Panel2Color else PanelColor
                        Surface(color = tint, shape = RoundedCornerShape(8.dp),
                                modifier = Modifier.fillMaxWidth()
                                    .padding(vertical = 3.dp)) {
                            Column(Modifier.padding(12.dp)) {
                                Text(name, color = TextColor, fontSize = 14.sp)
                                Text(path, color = DimColor, fontSize = 12.sp,
                                     fontFamily = FontFamily.Monospace)
                            }
                        }
                        Spacer(Modifier.height(2.dp))
                        TextButton(onClick = { chosen = path }) {
                            Text(if (chosen == path) "Выбрано" else "Выбрать")
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    Button(onClick = { model.connectUsb(chosen) },
                           enabled = !state.busy && chosen.isNotBlank(),
                           modifier = Modifier.fillMaxWidth()) {
                        Text("Подключиться")
                    }
                }
                Hint("Скорость обмена приложение подбирает само: перебирает " +
                     "38400, 9600, 115200 и 57600, пока адаптер не отзовётся. " +
                     "Если воткнуть адаптер при открытом телефоне, Android " +
                     "предложит открыть это приложение сам.")
            } else {
                if (state.bondedDevices.isEmpty()) {
                    Text("Сопряжённых устройств нет. Выполните сопряжение " +
                         "в настройках Bluetooth телефона, затем обновите список.",
                         color = DimColor, fontSize = 13.sp)
                    Spacer(Modifier.height(10.dp))
                    OutlinedButton(onClick = { model.loadBondedDevices() },
                                   modifier = Modifier.fillMaxWidth()) {
                        Text("Обновить список")
                    }
                } else {
                    state.bondedDevices.forEach { (address, name) ->
                        val tint = if (chosen == address) Panel2Color else PanelColor
                        Surface(color = tint, shape = RoundedCornerShape(8.dp),
                                modifier = Modifier.fillMaxWidth()
                                    .padding(vertical = 3.dp)) {
                            Column(Modifier.padding(12.dp)) {
                                Text(name, color = TextColor, fontSize = 14.sp)
                                Text(address, color = DimColor, fontSize = 12.sp,
                                     fontFamily = FontFamily.Monospace)
                            }
                        }
                        Spacer(Modifier.height(2.dp))
                        TextButton(onClick = { chosen = address }) {
                            Text(if (chosen == address) "Выбрано" else "Выбрать")
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    Button(onClick = { model.connectBluetooth(chosen) },
                           enabled = !state.busy && chosen.isNotBlank(),
                           modifier = Modifier.fillMaxWidth()) {
                        Text("Подключиться")
                    }
                }
                Hint("Bluetooth-адаптеры работают только в этом приложении: " +
                     "профиль последовательного порта недоступен из браузера " +
                     "и из Termux.")
            }

            if (state.connected) {
                Spacer(Modifier.height(10.dp))
                OutlinedButton(onClick = { model.disconnect() },
                               modifier = Modifier.fillMaxWidth()) { Text("Отключить") }
            }
        }

        if (state.warning.isNotBlank()) {
            Card { Text("⚠ ${state.warning}", color = WarnColor, fontSize = 13.sp) }
        }

        Card("Отчёт") {
            Row(Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically) {
                Text("Вкладывать полный обмен", color = TextColor, fontSize = 14.sp,
                     modifier = Modifier.weight(1f))
                Switch(checked = state.reportTrace,
                       onCheckedChange = { model.setReportTrace(it) })
            }
            Hint("Обмен с адаптером — сотни строк. Для обычного отчёта он не нужен, " +
                 "а мессенджер такое письмо обрежет. Включайте, когда отчёт идёт " +
                 "на разбор неисправности.")
        }

        Card("Журнал работы") {
            Surface(color = Color(0xFF0B0E12), shape = RoundedCornerShape(8.dp),
                    modifier = Modifier.fillMaxWidth().height(200.dp)) {
                Column(Modifier.padding(10.dp).verticalScroll(rememberScrollState())) {
                    state.log.takeLast(40).forEach {
                        Text(it, color = Color(0xFFC3CDD8), fontSize = 11.sp,
                             fontFamily = FontFamily.Monospace)
                    }
                }
            }
        }
    }
}

@Composable
fun ModulesScreen(state: AppState, model: DiagViewModel) {
    Column(Modifier.padding(12.dp)) {
        Card("Поиск блоков") {
            Row {
                Button(onClick = { model.scan(false) }, enabled = !state.busy,
                       modifier = Modifier.weight(1f)) { Text("Быстрый") }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = { model.scan(true) }, enabled = !state.busy,
                               modifier = Modifier.weight(1f)) { Text("Полный") }
            }
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = { model.collectAll() }, enabled = !state.busy,
                           modifier = Modifier.fillMaxWidth()) {
                Text("Собрать данные по всем")
            }
            Hint("Многоточие после имени или номера означает, что адаптер оборвал " +
                 "ответ на первом кадре. Так он ведёт себя со всеми блоками, " +
                 "кроме двигателя, — это ограничение железа, а не блока.")
        }

        LazyColumn(Modifier.weight(1f)) {
            items(state.modules) { item ->
                Card {
                    Row(Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(item.hex, color = TextColor, fontSize = 16.sp,
                             fontWeight = FontWeight.Bold,
                             fontFamily = FontFamily.Monospace)
                        Text(
                            when {
                                item.faults == null -> "—"
                                item.faults == 0 -> "ошибок нет"
                                else -> "ошибок: ${item.faults}"
                            },
                            color = when {
                                item.faults == null -> DimColor
                                item.faults == 0 -> AccentColor
                                else -> BadColor
                            }, fontSize = 14.sp)
                    }
                    if (item.name.isNotBlank()) Row2("Имя", item.name)
                    if (item.part.isNotBlank()) Row2("Номер", item.part)
                    if (item.rx.isNotBlank()) Row2("Ответ с", item.rx)
                    if (item.firstCode.isNotBlank()) Row2("Первый код", item.firstCode, WarnColor)

                    Spacer(Modifier.height(8.dp))
                    Row {
                        OutlinedButton(
                            onClick = { model.readFaults(item.canId) },
                            enabled = !state.busy, modifier = Modifier.weight(1f)) {
                            Text("Ошибки")
                        }
                        Spacer(Modifier.width(8.dp))
                        OutlinedButton(onClick = { model.clearFaults(item.canId) },
                                       enabled = !state.busy, modifier = Modifier.weight(1f),
                                       colors = ButtonDefaults.outlinedButtonColors(
                                           contentColor = BadColor)) {
                            Text("Стереть")
                        }
                    }

                    state.details["0x%03X".format(item.canId)]?.let { rows ->
                        Spacer(Modifier.height(10.dp))
                        Divider(color = LineColor)
                        Spacer(Modifier.height(8.dp))
                        rows.forEach { row ->
                            Row2("${row.title} (${row.count ?: "—"})",
                                 if (row.code.isBlank()) "—" else "${row.code} ${row.failure}",
                                 if ((row.count ?: 0) > 0) BadColor else AccentColor)
                        }
                    }
                }
            }
        }
    }
}

@Composable
fun EngineScreen(state: AppState, model: DiagViewModel) {
    Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
        Card("Двигатель, стандарт OBD-II") {
            Button(onClick = { model.readEngine() }, enabled = !state.busy,
                   modifier = Modifier.fillMaxWidth()) { Text("Прочитать") }
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = { model.clearEngine() }, enabled = !state.busy,
                           modifier = Modifier.fillMaxWidth(),
                           colors = ButtonDefaults.outlinedButtonColors(contentColor = BadColor)) {
                Text("Стереть коды и погасить лампу")
            }
            Hint("Стандартные режимы OBD-II есть у большинства легковых машин: " +
                 "в Европе они обязательны для бензиновых с 2001 года, " +
                 "для дизельных с 2004. У привезённых с других рынков разъём " +
                 "бывает на месте, а режимов нет — тогда вкладка промолчит.")
        }

        val engine = state.engine
        if (engine.read) {
            Card("Общие данные") {
                Row2("VIN", engine.vin.ifBlank { "—" })
                engine.readiness?.let {
                    Row2("Лампа неисправности", if (it.mil) "горит" else "погашена",
                         if (it.mil) BadColor else AccentColor)
                }
            }

            @Composable
            fun codeBlock(title: String, list: List<EngineCode>, note: String) {
                Card(title) {
                    if (list.isEmpty()) {
                        Text("нет", color = AccentColor, fontSize = 14.sp)
                    } else {
                        list.forEach {
                            Text(it.code, color = if (it.serious) BadColor else WarnColor,
                                 fontSize = 15.sp, fontWeight = FontWeight.Bold,
                                 fontFamily = FontFamily.Monospace)
                            Text(it.text, color = DimColor, fontSize = 13.sp,
                                 modifier = Modifier.padding(bottom = 8.dp))
                        }
                    }
                    Text(note, color = DimColor, fontSize = 12.sp)
                }
            }

            codeBlock("Сохранённые коды", engine.stored, "зажигают лампу неисправности")
            codeBlock("Неподтверждённые", engine.pending, "замечены один раз, ждут подтверждения")
            codeBlock("Постоянные", engine.permanent, "не стираются, пока причина не устранена")

            engine.readiness?.let { readiness ->
                Card("Готовность систем самодиагностики") {
                    readiness.checks.forEach { (title, value) ->
                        Row2(title, value, when (value) {
                            "готово" -> AccentColor
                            "не завершено" -> WarnColor
                            else -> DimColor
                        })
                    }
                    Hint("Непройденные тесты завалят техосмотр, даже если ошибок нет. " +
                         "После стирания кодов они сбрасываются и требуют пробега.")
                }
            }
        }
    }
}

@Composable
fun LiveScreen(state: AppState, model: DiagViewModel) {
    Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
        Card("Живые параметры") {
            Row {
                Button(onClick = { model.startLive() },
                       enabled = !state.liveRunning, modifier = Modifier.weight(1f)) {
                    Text("Запустить")
                }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = { model.stopLive() },
                               enabled = state.liveRunning, modifier = Modifier.weight(1f)) {
                    Text("Остановить")
                }
            }
            Hint("Приложение само спросит у двигателя, какие параметры он отдаёт, " +
                 "и будет опрашивать только их.")
        }

        state.live.forEach { value ->
            Card {
                Text(value.title, color = DimColor, fontSize = 13.sp)
                Text("${value.value} ${value.unit}", color = TextColor,
                     fontSize = 26.sp, fontWeight = FontWeight.Bold)
            }
        }
    }
}

@Composable
fun MonitorScreen(state: AppState, model: DiagViewModel) {
    var target by remember { mutableStateOf(0x70A) }
    var label by remember { mutableStateOf("") }

    Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
        Card("Блок для наблюдения") {
            Row(Modifier.horizontalScroll(rememberScrollState())) {
                state.modules.forEach { item ->
                    FilterChip(selected = target == item.canId,
                               onClick = { target = item.canId },
                               label = { Text(item.hex) },
                               modifier = Modifier.padding(end = 6.dp))
                }
            }
            if (state.modules.isEmpty()) {
                Text("Сначала выполните поиск блоков", color = DimColor, fontSize = 13.sp)
            }
        }

        Card("Монитор активных ошибок") {
            Row {
                Button(onClick = { model.startMonitor(target) },
                       enabled = !state.monitorRunning, modifier = Modifier.weight(1f)) {
                    Text("Запустить")
                }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = { model.stopMonitor() },
                               enabled = state.monitorRunning, modifier = Modifier.weight(1f)) {
                    Text("Остановить")
                }
            }
            Hint("Поиск неисправного датчика отключением:\n" +
                 "1. Задняя передача включена — без неё блок датчики не опрашивает\n" +
                 "2. Отключить все датчики бампера, запомнить число\n" +
                 "3. Подключать по одному, после каждого ждать 10–15 секунд\n" +
                 "4. Счётчик уменьшился — канал исправен. Не сдвинулся — вот дефект")
        }

        val last = state.monitor.lastOrNull()
        Row(Modifier.padding(vertical = 6.dp)) {
            Stat("Активных", last?.active?.toString() ?: "—",
                 if ((last?.active ?: 0) > 0) BadColor else AccentColor)
            Stat("Всего", last?.total?.toString() ?: "—")
        }
        if (!last?.code.isNullOrBlank()) {
            Card { Row2("Первый активный код", last!!.code, WarnColor) }
        }

        Card("Мастер поиска датчика") {
            OutlinedTextField(label, { label = it },
                              label = { Text("Что сделали на этом шаге") },
                              singleLine = true, modifier = Modifier.fillMaxWidth())
            Spacer(Modifier.height(8.dp))
            Row {
                Button(onClick = { model.wizardSample(target, label); label = "" },
                       enabled = !state.busy, modifier = Modifier.weight(1f)) {
                    Text("Снять показание")
                }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = { model.wizardReset() },
                               modifier = Modifier.weight(1f)) { Text("Сброс") }
            }
            state.wizard.forEach { sample ->
                Spacer(Modifier.height(6.dp))
                Row2("${sample.event.ifBlank { sample.time }}",
                     "активных ${sample.active ?: "—"}  ${sample.code}")
            }
        }

        state.monitor.reversed().take(20).forEach { sample ->
            Card {
                Row2(sample.time, "активных ${sample.active ?: "—"} · всего ${sample.total ?: "—"}",
                     if ((sample.active ?: 0) > 0) BadColor else AccentColor)
                if (sample.event.isNotBlank()) Row2("изменение", sample.event, WarnColor)
            }
        }
    }
}
