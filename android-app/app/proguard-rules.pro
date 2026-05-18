# MeshVision ProGuard rules
# Keep Mercury SDK
-keep class com.ffalcon.mercury.** { *; }
-dontwarn com.ffalcon.mercury.**

# Keep WebView JS bridge
-keepclassmembers class com.meshvision.MeshVisionActivity$MeshBridge {
    @android.webkit.JavascriptInterface <methods>;
}
