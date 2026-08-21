plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "ru.vagdiag"
    compileSdk = 36

    defaultConfig {
        applicationId = "ru.vagdiag"
        minSdk = 24                 // Android 7, охватывает почти все живые телефоны
        targetSdk = 36              // требование Google Play с 31.08.2026
        versionCode = 3
        versionName = "1.2"
    }

    // Подпись включается ТОЛЬКО когда задана переменная окружения
    // VAGDIAG_KEYSTORE. Не задана — release собирается неподписанным,
    // debug работает как прежде. Паролей в коде нет.
    val ksPath = System.getenv("VAGDIAG_KEYSTORE")
    signingConfigs {
        if (ksPath != null && file(ksPath).exists()) {
            create("release") {
                storeFile = file(ksPath)
                storePassword = System.getenv("VAGDIAG_STOREPASS")
                keyAlias = System.getenv("VAGDIAG_KEYALIAS") ?: "vagdiag"
                keyPassword = System.getenv("VAGDIAG_KEYPASS")
                // Все схемы подписи сразу: v1 понимают старые телефоны,
                // v2 — начиная с Android 7, v3 позволяет когда-нибудь
                // сменить ключ, не теряя установленную базу.
                enableV1Signing = true
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            if (ksPath != null && file(ksPath).exists()) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.17.0")
    implementation("androidx.activity:activity-compose:1.12.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.4")

    val composeBom = platform("androidx.compose:compose-bom:2025.09.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")

    // Драйверы CH340, FTDI, CP210x, PL2303 — микросхемы, которые стоят
    // внутри USB-адаптеров ELM327. Единственная внешняя зависимость.
    implementation("com.github.mik3y:usb-serial-for-android:3.11.0")
}
