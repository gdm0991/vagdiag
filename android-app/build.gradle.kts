// Версии вынесены сюда, чтобы обновлять их в одном месте.
// Подняты под compileSdk/targetSdk 36 — требование Google Play с 31.08.2026.
plugins {
    id("com.android.application") version "8.13.2" apply false
    id("org.jetbrains.kotlin.android") version "2.2.21" apply false
    // С Kotlin 2.0 компилятор Compose стал отдельным плагином,
    // kotlinCompilerExtensionVersion больше не задаётся руками.
    id("org.jetbrains.kotlin.plugin.compose") version "2.2.21" apply false
}
