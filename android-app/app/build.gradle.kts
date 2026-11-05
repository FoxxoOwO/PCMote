plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "com.pcmote"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.pcmote"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
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

    lint {
        // Lint suggests merging mipmap-anydpi-v26 into mipmap-anydpi because minSdk is 26,
        // but AAPT2 then fails with "resource mipmap/ic_launcher not found", so the version
        // qualifier has to stay.
        disable += "ObsoleteSdkInt"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

dependencies {
    // Core / lifecycle
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")

    // Compose + Material 3 Expressive
    // material3 1.5.0-alpha08 exposes MaterialExpressiveTheme, MotionScheme.expressive()
    // and the expressive component set as public API while still depending on Compose 1.8.x,
    // which is what AGP 8.7.x / compileSdk 35 support.
    implementation("androidx.compose.ui:ui:1.8.2")
    implementation("androidx.compose.ui:ui-graphics:1.8.2")
    implementation("androidx.compose.ui:ui-tooling-preview:1.8.2")
    implementation("androidx.compose.foundation:foundation:1.8.2")
    implementation("androidx.compose.material3:material3:1.5.0-alpha08")
    implementation("androidx.compose.material3:material3-window-size-class:1.5.0-alpha08")

    // Media3: MediaSessionService + SimpleBasePlayer
    implementation("androidx.media3:media3-session:1.4.1")
    implementation("androidx.media3:media3-common:1.4.1")

    // Coroutines
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // WebSocket + JSON
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.code.gson:gson:2.10.1")

    // Tests
    testImplementation("junit:junit:4.13.2")

    debugImplementation("androidx.compose.ui:ui-tooling:1.8.2")
}