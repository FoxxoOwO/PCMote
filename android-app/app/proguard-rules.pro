# PCMote ProGuard rules
-keep class com.pcmote.android.** { *; }
-keep class kotlinx.serialization.** { *; }
-keep class okhttp3.** { *; }
-keep class okio.** { *; }

# Media3
-keep class androidx.media3.** { *; }

# Hilt
-keep class dagger.hilt.** { *; }
-keep class com.google.dagger.hilt.** { *; }

# Keep annotations
-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod

# OkHttp / Okio
-dontwarn okhttp3.**
-dontwarn okio.**

# Kotlinx serialization
-dontwarn kotlinx.serialization.**

# NSD
-dontwarn androidx.nsd.**