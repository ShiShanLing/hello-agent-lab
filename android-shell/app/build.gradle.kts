plugins {
    id("com.android.application")
    kotlin("android")
}

val helloAgentBaseUrl = providers.gradleProperty("helloAgentBaseUrl")
    .orElse("https://shishanling.cn")
val workshopUrl = providers.gradleProperty("workshopUrl")
    .orElse("https://shishanling.cn/workshop/")

android {
    namespace = "com.shishanling.helloagentshell"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.shishanling.helloagentshell"
        minSdk = 31
        targetSdk = 36
        versionCode = 4
        versionName = "0.1.3"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "HELLO_AGENT_BASE_URL", "\"${helloAgentBaseUrl.get().trimEnd('/')}\"")
        buildConfigField("String", "WORKSHOP_URL", "\"${workshopUrl.get()}\"")
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
        buildConfig = true
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.10.0")
    implementation("androidx.activity:activity-ktx:1.8.2")
    implementation("androidx.webkit:webkit:1.8.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
