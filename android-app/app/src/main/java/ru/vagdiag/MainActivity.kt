package ru.vagdiag

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.enableEdgeToEdge
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import ru.vagdiag.ui.*

class MainActivity : ComponentActivity() {

    /**
     * Разрешения на Bluetooth. Начиная с Android 12 нужны новые,
     * до него — старые вместе с доступом к местоположению: система
     * считает список устройств поблизости сведениями о положении.
     */
    private val permissions =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            arrayOf(Manifest.permission.BLUETOOTH_CONNECT,
                    Manifest.permission.BLUETOOTH_SCAN)
        else
            arrayOf(Manifest.permission.BLUETOOTH,
                    Manifest.permission.BLUETOOTH_ADMIN,
                    Manifest.permission.ACCESS_FINE_LOCATION)

    private val askPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        // С targetSdk 35 и выше Android 15 всё равно рисует приложение
        // под строкой состояния. Объявляем это явно — тогда Scaffold
        // сам делает отступы, и содержимое не уезжает под вырез.
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        askPermissions.launch(permissions)

        setContent {
            MaterialTheme(colorScheme = DarkScheme) {
                AppScreen(onShare = ::shareReport)
            }
        }
    }

    /** Отправляет отчёт любым приложением: почтой, мессенджером, в файл. */
    private fun shareReport(text: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, "Отчёт диагностики")
            putExtra(Intent.EXTRA_TEXT, text)
        }
        startActivity(Intent.createChooser(intent, "Отправить отчёт"))
    }
}

private enum class Tab(
    val title: String,
    val icon: ImageVector,
) {
    Connect("Связь", Icons.Filled.Link),
    Modules("Блоки", Icons.Filled.Memory),
    Engine("Двигатель", Icons.Filled.Speed),
    Live("Данные", Icons.Filled.ShowChart),
    Monitor("Монитор", Icons.Filled.Search),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppScreen(onShare: (String) -> Unit) {
    val model: DiagViewModel = viewModel()
    val state by model.state.collectAsState()
    var tab by remember { mutableStateOf(Tab.Connect) }

    Scaffold(
        containerColor = BgColor,
        topBar = {
            Column {
                TopAppBar(
                    title = {
                        Column {
                            Text("Диагностика VAG", fontSize = 16.sp,
                                 fontWeight = FontWeight.SemiBold)
                            Text(
                                if (state.connected)
                                    "${state.adapterVersion} · ${state.voltage}"
                                else "не подключено",
                                fontSize = 12.sp,
                                color = if (state.connected) AccentColor else DimColor)
                        }
                    },
                    actions = {
                        IconButton(onClick = { onShare(model.buildReport()) }) {
                            Icon(Icons.Filled.Share, "Отправить отчёт", tint = DimColor)
                        }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(
                        containerColor = PanelColor, titleContentColor = TextColor)
                )
                if (state.busy) {
                    LinearProgressIndicator(
                        progress = { state.progress / 100f },
                        modifier = Modifier.fillMaxWidth(),
                        color = Accent2Color, trackColor = Panel2Color)
                    Text("${state.task}… ${state.progress}%", color = DimColor,
                         fontSize = 11.sp,
                         modifier = Modifier.padding(horizontal = 14.dp, vertical = 2.dp))
                } else if (state.message.isNotBlank()) {
                    Text(state.message, color = DimColor, fontSize = 11.sp,
                         modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp))
                }
            }
        },
        bottomBar = {
            NavigationBar(containerColor = PanelColor) {
                Tab.entries.forEach { item ->
                    NavigationBarItem(
                        selected = tab == item,
                        onClick = { tab = item },
                        icon = { Icon(item.icon, item.title) },
                        label = { Text(item.title, fontSize = 11.sp) },
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = Accent2Color,
                            selectedTextColor = Accent2Color,
                            unselectedIconColor = DimColor,
                            unselectedTextColor = DimColor,
                            indicatorColor = Panel2Color)
                    )
                }
            }
        }
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when (tab) {
                Tab.Connect -> ConnectScreen(state, model)
                Tab.Modules -> ModulesScreen(state, model)
                Tab.Engine -> EngineScreen(state, model)
                Tab.Live -> LiveScreen(state, model)
                Tab.Monitor -> MonitorScreen(state, model)
            }

            if (!state.connected && tab != Tab.Connect) {
                Surface(color = Color(0xCC0F1216), modifier = Modifier.fillMaxSize()) {
                    Column(Modifier.fillMaxSize(),
                           verticalArrangement = Arrangement.Center,
                           horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Filled.LinkOff, null, tint = DimColor,
                             modifier = Modifier.size(48.dp))
                        Spacer(Modifier.height(12.dp))
                        Text("Сначала подключитесь к адаптеру",
                             color = DimColor, fontSize = 14.sp)
                        Spacer(Modifier.height(12.dp))
                        Button(onClick = { tab = Tab.Connect }) { Text("К подключению") }
                    }
                }
            }
        }
    }
}
