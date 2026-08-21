pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Драйвер USB-serial живёт только тут: в Maven Central его нет.
        maven { url = uri("https://jitpack.io") }
    }
}

rootProject.name = "VAG Diag"
include(":app")
